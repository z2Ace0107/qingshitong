from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping


PROTOCOL_RESPONSES = "responses"
PROTOCOL_CHAT = "chat_completions"
OBSERVATION_STATES = {"unverified", "passed", "failed", "unsupported"}
DECLARATION_STATES = {"unverified", "declared", "unsupported"}
CAPABILITY_NAMES = (
    "responses",
    "chat_completions",
    "tools",
    "structured_output",
    "streaming",
    "cancellation",
    "error_semantics",
    "protocol_fallback",
)


@dataclass(frozen=True)
class ProtocolPolicy:
    """Server-owned protocol order and the only permitted fallback rule."""

    primary: str
    fallback: str

    @classmethod
    def responses_preferred(cls) -> "ProtocolPolicy":
        return cls(primary=PROTOCOL_RESPONSES, fallback=PROTOCOL_CHAT)

    def can_fallback(self, *, before_semantic_event: bool, reason: str) -> bool:
        return before_semantic_event and reason == "provider_capability_unsupported"


@dataclass(frozen=True)
class CapabilityMatrix(Mapping[str, dict[str, str]]):
    """Declared and observed capabilities; never contains credentials."""

    values: Mapping[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def unverified(cls) -> "CapabilityMatrix":
        return cls(
            MappingProxyType({
                name: {"declared": "unverified", "observed": "unverified"}
                for name in CAPABILITY_NAMES
            })
        )

    def with_observation(self, capability: str, state: str) -> "CapabilityMatrix":
        if capability not in CAPABILITY_NAMES:
            raise ValueError("unknown_provider_capability")
        if state not in OBSERVATION_STATES:
            raise ValueError("invalid_provider_capability_state")
        updated = {name: dict(value) for name, value in self.values.items()}
        updated.setdefault(capability, {"declared": "unverified", "observed": "unverified"})
        updated[capability]["observed"] = state
        return CapabilityMatrix(MappingProxyType(updated))

    def with_declaration(self, capability: str, state: str) -> "CapabilityMatrix":
        if capability not in CAPABILITY_NAMES:
            raise ValueError("unknown_provider_capability")
        if state not in DECLARATION_STATES:
            raise ValueError("invalid_provider_capability_state")
        updated = {name: dict(value) for name, value in self.values.items()}
        updated.setdefault(capability, {"declared": "unverified", "observed": "unverified"})
        updated[capability]["declared"] = state
        return CapabilityMatrix(MappingProxyType(updated))

    def __getitem__(self, key: str) -> dict[str, str]:
        return self.values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.values)

    def __len__(self) -> int:
        return len(self.values)

    def to_dict(self) -> dict[str, dict[str, str]]:
        return {name: dict(value) for name, value in self.values.items()}


@dataclass(frozen=True)
class ModelTurnRequest:
    turn_id: str
    instructions: str
    input: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    output_schema: dict[str, Any]
    stream: bool = False
    tool_choice: Any = None


@dataclass(frozen=True)
class NormalizedProviderError:
    category: str
    retryable: bool = False
    before_semantic_event: bool = True


@dataclass(frozen=True)
class ModelTurnResult:
    protocol: str
    text: str | None = None
    structured_payload: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None
    usage_summary: dict[str, Any] | None = None
    provider_error: NormalizedProviderError | None = None
    protocol_attempts: tuple[str, ...] = ()
    fallback_reason: str | None = None


@dataclass(frozen=True)
class NormalizedStreamEvent:
    kind: str
    sequence: int
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelGateway:
    """Protocol-neutral seam around the currently available provider transport."""

    chat_transport: Callable[..., Any]
    responses_transport: Callable[..., Any] | None = None
    policy: ProtocolPolicy = field(default_factory=ProtocolPolicy.responses_preferred)
    capabilities: CapabilityMatrix = field(default_factory=CapabilityMatrix.unverified)

    def complete(
        self,
        config: Any,
        request: ModelTurnRequest,
        *,
        protocol_lock: str | None = None,
        allow_fallback: bool | None = None,
        **kwargs: Any,
    ) -> ModelTurnResult:
        if protocol_lock not in {None, PROTOCOL_RESPONSES, PROTOCOL_CHAT}:
            raise ValueError("invalid_protocol_lock")
        if allow_fallback is None:
            allow_fallback = protocol_lock is None
        if protocol_lock is not None and allow_fallback:
            raise ValueError("locked_protocol_cannot_fallback")
        configured_protocol = getattr(config, "resolved_api_format", None)
        if protocol_lock == PROTOCOL_RESPONSES:
            prefer_responses = True
        elif protocol_lock == PROTOCOL_CHAT:
            prefer_responses = False
        else:
            prefer_responses = configured_protocol == PROTOCOL_RESPONSES or (
                configured_protocol is None and self.policy.primary == PROTOCOL_RESPONSES
            )
        if prefer_responses and self.responses_transport is not None:
            try:
                payload = self.responses_transport(config, request, **kwargs)
            except Exception as error:
                primary_result = _transport_error_result(error, PROTOCOL_RESPONSES)
            else:
                primary_result = normalize_responses_payload(payload)
            primary_result = replace(primary_result, protocol_attempts=(PROTOCOL_RESPONSES,))
            provider_error = primary_result.provider_error
            if (
                provider_error is not None
                and self.chat_transport is not None
                and allow_fallback
                and self.policy.can_fallback(
                    before_semantic_event=provider_error.before_semantic_event,
                    reason=provider_error.category,
                )
            ):
                try:
                    payload = self.chat_transport(config, request, **kwargs)
                except Exception as error:
                    fallback_result = _transport_error_result(error, PROTOCOL_CHAT)
                else:
                    fallback_result = normalize_chat_payload(payload)
                return replace(
                    fallback_result,
                    protocol_attempts=(PROTOCOL_RESPONSES, PROTOCOL_CHAT),
                    fallback_reason=provider_error.category,
                )
            return primary_result
        try:
            payload = self.chat_transport(config, request, **kwargs)
        except Exception as error:
            result = _transport_error_result(error, PROTOCOL_CHAT)
        else:
            result = normalize_chat_payload(payload)
        return replace(result, protocol_attempts=(PROTOCOL_CHAT,))

    def observe_capability(self, capability: str, state: str) -> CapabilityMatrix:
        self.capabilities = self.capabilities.with_observation(capability, state)
        return self.capabilities


def normalize_chat_payload(payload: Any) -> ModelTurnResult:
    if not isinstance(payload, dict):
        return ModelTurnResult(
            protocol=PROTOCOL_CHAT,
            provider_error=NormalizedProviderError("provider_invalid_response"),
        )
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ModelTurnResult(
            protocol=PROTOCOL_CHAT,
            provider_error=NormalizedProviderError("provider_invalid_response"),
        )
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        return ModelTurnResult(
            protocol=PROTOCOL_CHAT,
            provider_error=NormalizedProviderError("provider_invalid_response"),
        )
    normalized_tool_calls: list[dict[str, Any]] = []
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        if not isinstance(function, dict):
            continue
        normalized_tool_calls.append({
            "id": str(call.get("id") or "tool-call"),
            "name": function.get("name"),
            "arguments": function.get("arguments"),
        })
    text = message.get("content") if isinstance(message.get("content"), str) else None
    structured_payload = None
    if text:
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            structured_payload = parsed
    return ModelTurnResult(
        protocol=PROTOCOL_CHAT,
        text=text,
        structured_payload=structured_payload,
        tool_calls=normalized_tool_calls,
        finish_reason=choice.get("finish_reason"),
        usage_summary=payload.get("usage") if isinstance(payload.get("usage"), dict) else None,
    )


def _transport_error_result(error: Exception, protocol: str) -> ModelTurnResult:
    category = str(error.args[0]) if error.args and isinstance(error.args[0], str) else "provider_transport_error"
    known_categories = {
        "cancelled",
        "deadline_exceeded",
        "provider_timeout",
        "provider_unavailable",
        "provider_rate_limited",
        "provider_unauthorized",
        "provider_http_error",
        "provider_capability_unsupported",
        "agent_provider_unavailable",
    }
    if category not in known_categories:
        category = "provider_transport_error"
    return ModelTurnResult(
        protocol=protocol,
        provider_error=NormalizedProviderError(
            category=category,
            retryable=category in {"provider_timeout", "provider_unavailable", "provider_rate_limited", "agent_provider_unavailable", "provider_transport_error"},
            before_semantic_event=True,
        ),
    )


def normalize_responses_payload(payload: Any) -> ModelTurnResult:
    if not isinstance(payload, dict) or not isinstance(payload.get("output"), list):
        return ModelTurnResult(
            protocol=PROTOCOL_RESPONSES,
            provider_error=NormalizedProviderError("provider_invalid_response"),
        )
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in payload["output"]:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            tool_calls.append({
                "id": str(item.get("call_id") or item.get("id") or "tool-call"),
                "name": item.get("name"),
                "arguments": item.get("arguments"),
            })
        elif item.get("type") == "message":
            for content in item.get("content") or []:
                if isinstance(content, dict) and content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                    text_parts.append(content["text"])
        elif item.get("type") == "output_text" and isinstance(item.get("text"), str):
            text_parts.append(item["text"])
    text = "".join(text_parts) or None
    structured_payload = None
    if text:
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            structured_payload = parsed
    return ModelTurnResult(
        protocol=PROTOCOL_RESPONSES,
        text=text,
        structured_payload=structured_payload,
        tool_calls=tool_calls,
        finish_reason=payload.get("status"),
        usage_summary=payload.get("usage") if isinstance(payload.get("usage"), dict) else None,
    )


def responses_tool_definitions(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    definitions: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            continue
        definitions.append({
            "type": "function",
            "name": function["name"],
            "description": function.get("description", ""),
            "parameters": function.get("parameters", {"type": "object"}),
        })
    return definitions
