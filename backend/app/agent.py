from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from .model_gateway import (
    PROTOCOL_CHAT,
    PROTOCOL_RESPONSES,
    CapabilityMatrix,
    ModelGateway,
    ModelTurnRequest,
    responses_tool_definitions,
)
from .provider_stream import (
    ProviderStreamError,
    iter_provider_http_events_with_retry,
    response_indicates_capability_unsupported,
)


PROVIDER_BASE_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
}
DEFAULT_MODELS = {
    "openrouter": "nex-agi/nex-n2.5-mini:free",
    "compatible": "compatible-model",
}
DEFAULT_API_FORMATS = {
    "openrouter": "chat_completions",
    "compatible": "chat_completions",
}
RESPONSES_REQUEST_VARIANTS = {"standard", "message_input_max_tokens"}
CHAT_REASONING_EFFORTS = {"none", "low", "medium", "high", "max"}
TOOL_ID = "search_service_items"
ANSWER_DRAFT_INSTRUCTION = (
    "请根据上面的检索结果生成回答草稿。只能返回一个 JSON 对象，不要输出解释、Markdown 或代码围栏。"
    "JSON 必须包含 response_kind、service_item_ref、summary、claims、next_actions 五个字段。"
    "response_kind 必须是 answer、clarification、refusal 或 degraded 之一；"
    "service_item_ref 必须使用检索结果中的事项 slug；"
    "claims 必须是数组，每项包含 text 和 evidence_refs，evidence_refs 只能引用检索结果中的事项 slug；"
    "next_actions 必须是字符串数组。"
)
ANSWER_DRAFT_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["response_kind", "service_item_ref", "summary", "claims", "next_actions"],
    "properties": {
        "response_kind": {"type": "string", "enum": ["answer", "clarification", "refusal", "degraded"]},
        "service_item_ref": {"type": "string", "minLength": 1},
        "summary": {"type": "string", "minLength": 1, "maxLength": 600},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "evidence_refs"],
                "properties": {
                    "text": {"type": "string", "minLength": 1},
                    "evidence_refs": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1},
                },
            },
        },
        "next_actions": {"type": "array", "items": {"type": "string", "maxLength": 120}, "maxItems": 5},
    },
}
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_RUN_TIMEOUT_SECONDS = 45.0
MAX_CONTEXT_TOKENS = 12_000
MAX_RETRY_AFTER_SECONDS = 2.0
DEFAULT_MAX_TURNS = 2
DEFAULT_MAX_OUTPUT_TOKENS = 320
DEFAULT_RETRY_COUNT = 1
SUPPORTED_SERVER_PROVIDERS = {"openrouter", "compatible"}
PUBLIC_PROVIDER_HOSTS = {"openrouter": {"openrouter.ai", "www.openrouter.ai"}}
PRIVATE_HOST_NAMES = {"localhost", "metadata", "metadata.google.internal", "host.docker.internal"}


class ProviderConfigurationError(RuntimeError):
    """A server configuration error that must stop an unsafe deployment."""


class AgentModelError(RuntimeError):
    """A provider failure that must degrade without exposing provider details."""


@dataclass(frozen=True)
class AgentConfig:
    provider: str = "deterministic"
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_format: str | None = None
    # Direct callers remain Chat-compatible unless a server-owned Profile opts into Responses.
    protocol_policy: str = "chat_compat"
    responses_request_variant: str = "standard"
    chat_reasoning_effort: str | None = None
    timeout_seconds: float = 18.0
    run_timeout_seconds: float = DEFAULT_RUN_TIMEOUT_SECONDS
    max_turns: int = 2
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    retry_count: int = DEFAULT_RETRY_COUNT
    allow_private_endpoint: bool = False
    deployment_mode: str = "development"

    @property
    def enabled(self) -> bool:
        return bool(
            self.api_key
            and self.provider in SUPPORTED_SERVER_PROVIDERS
            and self.resolved_base_url
            and self.resolved_api_format in {"responses", "chat_completions"}
            and _endpoint_error(
                self.resolved_base_url,
                provider=self.provider,
                allow_private_endpoint=self.allow_private_endpoint,
                deployment_mode=self.deployment_mode,
            ) is None
        )

    @property
    def resolved_model(self) -> str:
        return (self.model or DEFAULT_MODELS.get(self.provider) or "").strip()

    @property
    def resolved_base_url(self) -> str:
        candidate = (self.base_url or PROVIDER_BASE_URLS.get(self.provider) or "").strip().rstrip("/")
        parsed = urlparse(candidate)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            return ""
        return candidate

    @property
    def resolved_api_format(self) -> str:
        path = urlparse(self.resolved_base_url).path.rstrip("/")
        if path.endswith("/chat/completions"):
            return "chat_completions"
        if path.endswith("/responses"):
            return "responses"
        candidate = (self.api_format or "").strip()
        if candidate in {"responses", "chat_completions"}:
            return candidate
        if self.protocol_policy == "responses_preferred":
            return "responses"
        return (DEFAULT_API_FORMATS.get(self.provider) or "").strip()


@dataclass(frozen=True)
class ProviderProfile:
    """One server-owned LLM profile for the current deployment."""

    provider: str = "deterministic"
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None
    protocol: str = "responses_preferred"
    responses_request_variant: str = "standard"
    chat_reasoning_effort: str | None = None
    capabilities: CapabilityMatrix = field(default_factory=CapabilityMatrix.unverified)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    run_timeout_seconds: float = DEFAULT_RUN_TIMEOUT_SECONDS
    max_turns: int = DEFAULT_MAX_TURNS
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    retry_count: int = DEFAULT_RETRY_COUNT
    secret_source: str = "none"
    config_error: str | None = None
    allow_private_endpoint: bool = False
    deployment_mode: str = "development"

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.provider != "deterministic" and self.base_url and not self.config_error)

    def to_agent_config(self) -> AgentConfig:
        return AgentConfig(
            provider=self.provider,
            api_key=self.api_key,
            model=self.model,
            base_url=self.base_url,
            api_format=None,
            protocol_policy=self.protocol,
            responses_request_variant=self.responses_request_variant,
            chat_reasoning_effort=self.chat_reasoning_effort,
            timeout_seconds=self.timeout_seconds,
            run_timeout_seconds=self.run_timeout_seconds,
            max_turns=self.max_turns,
            max_output_tokens=self.max_output_tokens,
            retry_count=self.retry_count,
            allow_private_endpoint=self.allow_private_endpoint,
            deployment_mode=self.deployment_mode,
        )

    def with_capability_observation(self, capability: str, state: str) -> "ProviderProfile":
        return replace(self, capabilities=self.capabilities.with_observation(capability, state))

    def to_health_payload(self) -> dict[str, Any]:
        if self.config_error:
            status = "degraded"
            reason = self.config_error
        elif self.configured:
            status = "ready"
            reason = None
        else:
            status = "degraded"
            reason = "provider_not_configured"
        return {
            "status": status,
            "configured": self.configured,
            "provider": self.provider if self.configured else None,
            "protocol": self.protocol if self.configured else None,
            "capabilities": self.capabilities.to_dict(),
            "secret_source": self.secret_source,
            "reason": reason,
        }


def _env_float(env: dict[str, str], name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _env_int(env: dict[str, str], name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _declared_capabilities(env: dict[str, str]) -> CapabilityMatrix:
    matrix = CapabilityMatrix.unverified()
    raw = (env.get("QST_LLM_DECLARED_CAPABILITIES") or "").strip()
    if not raw:
        return matrix
    try:
        values = json.loads(raw)
    except (TypeError, ValueError):
        return matrix
    if not isinstance(values, dict):
        return matrix
    for capability, state in values.items():
        if isinstance(capability, str) and isinstance(state, str):
            try:
                matrix = matrix.with_declaration(capability, state)
            except ValueError:
                continue
    return matrix


def _responses_request_variant(env: dict[str, str]) -> tuple[str, str | None]:
    value = (env.get("QST_LLM_RESPONSES_REQUEST_VARIANT") or "standard").strip().lower()
    if value not in RESPONSES_REQUEST_VARIANTS:
        return "standard", "provider_responses_request_variant_invalid"
    return value, None


def _chat_reasoning_effort(env: dict[str, str]) -> tuple[str | None, str | None]:
    value = (env.get("QST_LLM_CHAT_REASONING_EFFORT") or "").strip().lower()
    if not value:
        return None, None
    if value not in CHAT_REASONING_EFFORTS:
        return None, "provider_chat_reasoning_effort_invalid"
    return value, None


def _endpoint_error(
    candidate: str,
    *,
    provider: str,
    allow_private_endpoint: bool = False,
    deployment_mode: str = "development",
) -> str | None:
    parsed = urlparse(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return "provider_base_url_invalid"
    if parsed.scheme != "https" and not allow_private_endpoint:
        return "provider_endpoint_not_allowed"

    hostname = parsed.hostname.rstrip(".").lower()
    if deployment_mode in {"public", "public_demo"}:
        allowed_hosts = PUBLIC_PROVIDER_HOSTS.get(provider, set())
        if hostname not in allowed_hosts:
            return "provider_endpoint_not_allowed"

    private = hostname in PRIVATE_HOST_NAMES or hostname.endswith(".local")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None:
        private = (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or str(address) in {"169.254.169.254", "100.100.100.200"}
        )
    if private and not allow_private_endpoint:
        return "provider_endpoint_not_allowed"
    return None


def _read_secret_file(path_value: str | None) -> str | None:
    if not path_value:
        return None
    path = Path(path_value)
    try:
        if not path.is_file():
            return None
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def load_provider_profile(environ: dict[str, str] | None = None) -> ProviderProfile:
    """Load the single server-owned profile without exposing secret contents."""
    env = dict(os.environ if environ is None else environ)
    app_env = (env.get("QST_APP_ENV") or "development").strip().lower()
    responses_request_variant, variant_error = _responses_request_variant(env)
    chat_reasoning_effort, reasoning_error = _chat_reasoning_effort(env)
    allow_private_endpoint = _truthy(env.get("QST_ALLOW_PRIVATE_PROVIDER_ENDPOINT")) and app_env not in {"public", "public_demo"}
    env_secret = (env.get("QST_LLM_API_KEY") or "").strip() or None
    secret_path = (env.get("QST_LLM_API_KEY_FILE") or "/run/secrets/qst_llm_api_key").strip()
    file_secret = _read_secret_file(secret_path)
    if file_secret and env_secret and file_secret != env_secret and app_env == "production":
        raise ProviderConfigurationError("secret_conflict")
    api_key = file_secret or env_secret
    secret_source = "secret_file" if file_secret else "environment" if env_secret else "none"

    requested_provider = (env.get("QST_LLM_PROVIDER") or "").strip().lower()
    provider = requested_provider or ("openrouter" if api_key else "deterministic")
    if provider not in {"deterministic", *SUPPORTED_SERVER_PROVIDERS}:
        provider = "deterministic"
        config_error = "provider_unsupported"
    else:
        config_error = None
    config_error = config_error or variant_error or reasoning_error

    base_url = (env.get("QST_LLM_BASE_URL") or PROVIDER_BASE_URLS.get(provider) or "").strip().rstrip("/") or None
    model = (env.get("QST_LLM_MODEL") or DEFAULT_MODELS.get(provider) or "").strip() or None
    if provider == "compatible" and not base_url and api_key:
        config_error = config_error or "provider_base_url_missing"
    if provider != "deterministic" and base_url:
        parsed = urlparse(base_url)
        if not parsed.netloc:
            config_error = config_error or "provider_base_url_invalid"
            base_url = None
        else:
            endpoint_error = _endpoint_error(
                base_url,
                provider=provider,
                allow_private_endpoint=allow_private_endpoint,
                deployment_mode=app_env,
            )
            if endpoint_error:
                config_error = config_error or endpoint_error
                base_url = None
    if provider != "deterministic" and not model:
        config_error = config_error or "provider_model_missing"

    return ProviderProfile(
        provider=provider,
        api_key=api_key,
        model=model,
        base_url=base_url,
        protocol="responses_preferred",
        responses_request_variant=responses_request_variant,
        chat_reasoning_effort=chat_reasoning_effort,
        capabilities=_declared_capabilities(env),
        timeout_seconds=_env_float(env, "QST_LLM_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, minimum=1.0, maximum=20.0),
        run_timeout_seconds=_env_float(env, "QST_LLM_RUN_TIMEOUT_SECONDS", DEFAULT_RUN_TIMEOUT_SECONDS, minimum=1.0, maximum=DEFAULT_RUN_TIMEOUT_SECONDS),
        max_turns=_env_int(env, "QST_LLM_MAX_TURNS", DEFAULT_MAX_TURNS, minimum=1, maximum=2),
        max_output_tokens=_env_int(env, "QST_LLM_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS, minimum=32, maximum=320),
        retry_count=_env_int(env, "QST_LLM_RETRY_COUNT", DEFAULT_RETRY_COUNT, minimum=0, maximum=1),
        secret_source=secret_source,
        config_error=config_error,
        allow_private_endpoint=allow_private_endpoint,
        deployment_mode=app_env,
    )


SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": TOOL_ID,
        "description": "按用户原意检索已发布的校园事项证据。只能读取虚拟演示资料，不能查询个人数据或提交业务。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 2000, "description": "用于事项检索的自然语言问题"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}

def _input_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _provider_url(config: AgentConfig, protocol: str = "chat_completions") -> str:
    base_url = config.resolved_base_url
    path = urlparse(base_url).path.rstrip("/")
    endpoint = "/responses" if protocol == "responses" else "/chat/completions"
    for suffix in ("/chat/completions", "/responses"):
        if path.endswith(suffix):
            return f"{base_url[:-len(suffix)]}{endpoint}"
    return f"{base_url}{endpoint}"


def _retry_after_seconds(value: str | None) -> float:
    try:
        return max(0.0, min(MAX_RETRY_AFTER_SECONDS, float(value or 0)))
    except (TypeError, ValueError):
        return 0.0


def _remaining_seconds(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return deadline - time.monotonic()


def _raise_if_cancelled(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise AgentModelError("cancelled")


def _post_provider_json(
    config: AgentConfig,
    body: dict[str, Any],
    *,
    deadline: float | None = None,
    cancel_event: Event | None = None,
    protocol: str = "chat_completions",
) -> dict[str, Any]:
    if not config.enabled or not config.resolved_model:
        raise AgentModelError("agent_config_invalid")
    if protocol == "responses" and config.resolved_api_format != "responses":
        raise AgentModelError("agent_config_invalid")
    if protocol == "chat_completions" and config.resolved_api_format != "chat_completions" and config.protocol_policy != "responses_preferred":
        raise AgentModelError("agent_config_invalid")
    transient_statuses = {429, 502, 503, 504}
    last_error: AgentModelError | None = None
    for attempt in range(config.retry_count + 1):
        _raise_if_cancelled(cancel_event)
        remaining = _remaining_seconds(deadline)
        if remaining is not None and remaining <= 0:
            raise AgentModelError("deadline_exceeded")
        timeout = min(config.timeout_seconds, remaining) if remaining is not None else config.timeout_seconds
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                response = client.post(
                    _provider_url(config, protocol),
                    headers={
                        "Authorization": f"Bearer {config.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
                _raise_if_cancelled(cancel_event)
                status_code = response.status_code
                if status_code in {401, 403}:
                    raise AgentModelError("provider_unauthorized")
                if response_indicates_capability_unsupported(response, protocol):
                    raise AgentModelError("provider_capability_unsupported")
                if status_code == 429:
                    last_error = AgentModelError("provider_rate_limited")
                    if attempt < config.retry_count:
                        _raise_if_cancelled(cancel_event)
                        delay = _retry_after_seconds(response.headers.get("Retry-After"))
                        remaining = _remaining_seconds(deadline)
                        if remaining is not None and delay >= remaining:
                            raise AgentModelError("deadline_exceeded")
                        if delay:
                            time.sleep(delay)
                        continue
                    raise last_error
                if status_code in transient_statuses:
                    last_error = AgentModelError("provider_unavailable")
                    if attempt < config.retry_count:
                        _raise_if_cancelled(cancel_event)
                        delay = _retry_after_seconds(response.headers.get("Retry-After"))
                        remaining = _remaining_seconds(deadline)
                        if remaining is not None and delay >= remaining:
                            raise AgentModelError("deadline_exceeded")
                        if delay:
                            time.sleep(delay)
                        continue
                    raise last_error
                if 300 <= status_code < 400:
                    raise AgentModelError("provider_redirect_blocked")
                if status_code >= 400:
                    raise AgentModelError("provider_http_error")
                payload = response.json()
        except AgentModelError:
            raise
        except httpx.TimeoutException as error:
            _raise_if_cancelled(cancel_event)
            last_error = AgentModelError("provider_timeout")
            if attempt < config.retry_count:
                continue
            raise last_error from error
        except httpx.RequestError as error:
            _raise_if_cancelled(cancel_event)
            last_error = AgentModelError("provider_unavailable")
            if attempt < config.retry_count:
                continue
            raise last_error from error
        except (ValueError, TypeError) as error:
            raise AgentModelError("provider_invalid_response") from error
        if not isinstance(payload, dict):
            raise AgentModelError("provider_invalid_response")
        return payload
    raise last_error or AgentModelError("provider_unavailable")


def _chat_completion(
    config: AgentConfig,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: dict[str, Any] | str | None = None,
    response_format: dict[str, Any] | None = None,
    deadline: float | None = None,
    cancel_event: Event | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": config.resolved_model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": config.max_output_tokens,
    }
    if tools:
        body["tools"] = tools
        body["parallel_tool_calls"] = False
    if config.chat_reasoning_effort is not None:
        body["reasoning_effort"] = config.chat_reasoning_effort
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    if response_format is not None:
        body["response_format"] = response_format
    payload = _post_provider_json(config, body, deadline=deadline, cancel_event=cancel_event)
    if not isinstance(payload, dict) or not payload.get("choices"):
        raise AgentModelError("agent_provider_invalid_response")
    return payload


def _responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            for call in message["tool_calls"]:
                function = call.get("function") or {}
                items.append({
                    "type": "function_call",
                    "call_id": str(call.get("id") or "tool-call-1"),
                    "name": function.get("name"),
                    "arguments": function.get("arguments") or "{}",
                })
            if message.get("content"):
                items.append({"role": "assistant", "content": message["content"]})
        elif role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": str(message.get("tool_call_id") or "tool-call-1"),
                "output": message.get("content") or "",
            })
        else:
            items.append({"role": role, "content": message.get("content") or ""})
    return items


def _responses_request_input(request: ModelTurnRequest, config: AgentConfig) -> tuple[str | None, list[dict[str, Any]]]:
    if config.responses_request_variant == "message_input_max_tokens":
        return None, _responses_input([{"role": "system", "content": request.instructions}, *request.input])
    return request.instructions, _responses_input(request.input)


def _responses_tool_choice(tool_choice: Any) -> Any:
    if isinstance(tool_choice, dict):
        function = tool_choice.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            return {"type": "function", "name": function["name"]}
    return tool_choice


def _chat_output_format(output_schema: dict[str, Any]) -> dict[str, Any] | None:
    if not output_schema:
        return None
    if output_schema.get("type") == "json_object":
        return {"type": "json_object"}
    return {"type": "json_object"}


def _responses_output_format(output_schema: dict[str, Any]) -> dict[str, Any] | None:
    if not output_schema:
        return None
    if output_schema.get("type") == "json_schema":
        return output_schema
    schema = output_schema if output_schema.get("type") == "object" else ANSWER_DRAFT_JSON_SCHEMA
    return {
        "type": "json_schema",
        "name": "answer_draft",
        "strict": True,
        "schema": schema,
    }


def _responses_completion(
    config: AgentConfig,
    request: ModelTurnRequest,
    *,
    deadline: float | None = None,
    cancel_event: Event | None = None,
) -> dict[str, Any]:
    if request.stream:
        return _responses_completion_stream(config, request, deadline=deadline, cancel_event=cancel_event)
    instructions, response_input = _responses_request_input(request, config)
    body: dict[str, Any] = {
        "model": config.resolved_model,
        "input": response_input,
    }
    if instructions is not None:
        body["instructions"] = instructions
    if config.responses_request_variant == "message_input_max_tokens":
        body["max_tokens"] = config.max_output_tokens
    else:
        body["temperature"] = 0
        body["max_output_tokens"] = config.max_output_tokens
    if request.tools:
        body["tools"] = responses_tool_definitions(request.tools)
        body["parallel_tool_calls"] = False
    if request.tool_choice is not None:
        body["tool_choice"] = _responses_tool_choice(request.tool_choice)
    output_format = _responses_output_format(request.output_schema)
    if output_format is not None:
        body["text"] = {"format": output_format}
    payload = _post_provider_json(config, body, deadline=deadline, cancel_event=cancel_event, protocol="responses")
    if not isinstance(payload, dict) or not isinstance(payload.get("output"), list):
        raise AgentModelError("agent_provider_invalid_response")
    return payload


def _responses_completion_stream(
    config: AgentConfig,
    request: ModelTurnRequest,
    *,
    deadline: float | None = None,
    cancel_event: Event | None = None,
) -> dict[str, Any]:
    """Consume a Responses SSE stream and rebuild one safe model turn."""
    if not config.enabled or not config.resolved_model or config.resolved_api_format != "responses":
        raise AgentModelError("agent_config_invalid")
    instructions, response_input = _responses_request_input(request, config)
    body: dict[str, Any] = {
        "model": config.resolved_model,
        "input": response_input,
        "stream": True,
    }
    if instructions is not None:
        body["instructions"] = instructions
    if config.responses_request_variant == "message_input_max_tokens":
        body["max_tokens"] = config.max_output_tokens
    else:
        body["temperature"] = 0
        body["max_output_tokens"] = config.max_output_tokens
    if request.tools:
        body["tools"] = responses_tool_definitions(request.tools)
        body["parallel_tool_calls"] = False
    if request.tool_choice is not None:
        body["tool_choice"] = _responses_tool_choice(request.tool_choice)
    output_format = _responses_output_format(request.output_schema)
    if output_format is not None:
        body["text"] = {"format": output_format}

    _raise_if_cancelled(cancel_event)
    remaining = _remaining_seconds(deadline)
    if remaining is not None and remaining <= 0:
        raise AgentModelError("deadline_exceeded")
    timeout = min(config.timeout_seconds, remaining) if remaining is not None else config.timeout_seconds
    text_parts: list[str] = []
    tool_calls: dict[str, dict[str, str]] = {}
    finish_reason: str | None = None
    usage: dict[str, int] | None = None
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            events = iter_provider_http_events_with_retry(
                client,
                url=_provider_url(config, "responses"),
                headers={
                    "Authorization": f"Bearer {config.api_key}",
                    "Content-Type": "application/json",
                },
                payload=body,
                provider=config.provider,
                protocol="responses",
                cancel_event=cancel_event,
                timeout=timeout,
                max_retries=min(config.retry_count, 1),
            )
            for event in events:
                _raise_if_cancelled(cancel_event)
                if event.kind == "text_delta" and event.text:
                    text_parts.append(event.text)
                elif event.kind in {"lifecycle", "tool_call_delta"} and (event.tool_call_id or event.tool_name or event.tool_arguments):
                    call_key = event.tool_call_id or "tool-call-1"
                    call = tool_calls.setdefault(call_key, {"name": "", "arguments": ""})
                    if event.tool_name:
                        call["name"] = event.tool_name
                    if event.tool_arguments:
                        call["arguments"] += event.tool_arguments
                elif event.kind == "usage":
                    usage = event.usage
                elif event.kind == "upstream_end":
                    finish_reason = event.finish_reason
                    if event.usage is not None:
                        usage = event.usage
                elif event.kind == "error":
                    raise AgentModelError("provider_stream_error")
    except AgentModelError:
        raise
    except ProviderStreamError as error:
        code_map = {
            "provider_cancelled": "cancelled",
            "provider_connect_timeout": "provider_timeout",
            "provider_timeout_after_headers": "provider_timeout",
            "provider_connect_error": "provider_unavailable",
            "provider_stream_read_error": "provider_unavailable",
            "provider_rate_limited": "provider_rate_limited",
            "provider_unauthorized": "provider_unauthorized",
            "provider_unavailable": "provider_unavailable",
            "provider_capability_unsupported": "provider_capability_unsupported",
        }
        raise AgentModelError(code_map.get(error.code, "provider_invalid_response")) from error
    except httpx.TimeoutException as error:
        _raise_if_cancelled(cancel_event)
        raise AgentModelError("provider_timeout") from error
    except httpx.RequestError as error:
        _raise_if_cancelled(cancel_event)
        raise AgentModelError("provider_unavailable") from error

    if finish_reason != "completed":
        raise AgentModelError("provider_stream_incomplete")

    output: list[dict[str, Any]] = []
    for call_id, call in tool_calls.items():
        output.append({
            "type": "function_call",
            "call_id": call_id,
            "name": call["name"],
            "arguments": call["arguments"],
        })
    if text_parts:
        output.append({"type": "message", "content": [{"type": "output_text", "text": "".join(text_parts)}]})
    result: dict[str, Any] = {
        "status": "completed" if finish_reason == "completed" else "incomplete",
        "output": output,
    }
    if usage is not None:
        result["usage"] = usage
    return result


def _chat_completion_stream(
    config: AgentConfig,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: dict[str, Any] | str | None = None,
    response_format: dict[str, Any] | None = None,
    deadline: float | None = None,
    cancel_event: Event | None = None,
) -> dict[str, Any]:
    """Consume a Chat Completions SSE stream and rebuild one safe model turn."""
    if not config.enabled or not config.resolved_model or (
        config.resolved_api_format != "chat_completions"
        and config.protocol_policy != "responses_preferred"
    ):
        raise AgentModelError("agent_config_invalid")
    body: dict[str, Any] = {
        "model": config.resolved_model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": config.max_output_tokens,
        "stream": True,
    }
    if tools:
        body["tools"] = tools
        body["parallel_tool_calls"] = False
    if config.chat_reasoning_effort is not None:
        body["reasoning_effort"] = config.chat_reasoning_effort
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    if response_format is not None:
        body["response_format"] = response_format

    _raise_if_cancelled(cancel_event)
    remaining = _remaining_seconds(deadline)
    if remaining is not None and remaining <= 0:
        raise AgentModelError("deadline_exceeded")
    timeout = min(config.timeout_seconds, remaining) if remaining is not None else config.timeout_seconds
    text_parts: list[str] = []
    tool_calls: dict[str, dict[str, str]] = {}
    tool_call_ids_by_index: dict[int, str] = {}
    finish_reason: str | None = None
    usage: dict[str, int] | None = None
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            events = iter_provider_http_events_with_retry(
                client,
                url=_provider_url(config),
                headers={
                    "Authorization": f"Bearer {config.api_key}",
                    "Content-Type": "application/json",
                },
                payload=body,
                provider=config.provider,
                protocol="chat_completions",
                cancel_event=cancel_event,
                timeout=timeout,
                max_retries=min(config.retry_count, 1),
            )
            for event in events:
                _raise_if_cancelled(cancel_event)
                if event.kind == "text_delta" and event.text:
                    text_parts.append(event.text)
                elif event.kind == "tool_call_delta":
                    tool_call_index = getattr(event, "tool_call_index", None)
                    if tool_call_index is not None and event.tool_call_id:
                        tool_call_ids_by_index.setdefault(tool_call_index, event.tool_call_id)
                    if tool_call_index is not None:
                        call_key = (
                            tool_call_ids_by_index.get(tool_call_index)
                            or event.tool_call_id
                            or f"tool-call-{tool_call_index + 1}"
                        )
                    else:
                        call_key = event.tool_call_id or "tool-call-1"
                    call = tool_calls.setdefault(call_key, {"name": "", "arguments": ""})
                    if event.tool_name:
                        call["name"] = event.tool_name
                    if event.tool_arguments:
                        call["arguments"] += event.tool_arguments
                elif event.kind == "usage":
                    usage = event.usage
                elif event.kind == "upstream_end":
                    finish_reason = event.finish_reason
                elif event.kind == "error":
                    raise AgentModelError("provider_stream_error")
    except AgentModelError:
        raise
    except ProviderStreamError as error:
        code_map = {
            "provider_cancelled": "cancelled",
            "provider_connect_timeout": "provider_timeout",
            "provider_timeout_after_headers": "provider_timeout",
            "provider_connect_error": "provider_unavailable",
            "provider_stream_read_error": "provider_unavailable",
            "provider_rate_limited": "provider_rate_limited",
            "provider_unauthorized": "provider_unauthorized",
            "provider_unavailable": "provider_unavailable",
        }
        raise AgentModelError(code_map.get(error.code, "provider_invalid_response")) from error
    except httpx.TimeoutException as error:
        _raise_if_cancelled(cancel_event)
        raise AgentModelError("provider_timeout") from error
    except httpx.RequestError as error:
        _raise_if_cancelled(cancel_event)
        raise AgentModelError("provider_unavailable") from error

    message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": call["name"], "arguments": call["arguments"]},
            }
            for call_id, call in tool_calls.items()
        ]
    result: dict[str, Any] = {
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if usage is not None:
        result["usage"] = usage
    return result


def _assistant_message(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as error:
        raise AgentModelError("agent_provider_invalid_response") from error
    if not isinstance(message, dict):
        raise AgentModelError("agent_provider_invalid_response")
    return message


def _compact_search_result(result: dict[str, Any]) -> dict[str, Any]:
    selected = result.get("selected") or {}
    candidates = []
    for item in (result.get("candidates") or [])[:3]:
        candidates.append(
            {
                "slug": item.get("slug"),
                "title": item.get("title"),
                "score": item.get("score"),
                "source_revision_id": item.get("source_revision_id"),
                "freshness_state": item.get("freshness_state"),
            }
        )
    return {
        "selected": {
            "slug": selected.get("slug"),
            "title": selected.get("title"),
            "source_revision_id": selected.get("source_revision_id"),
        },
        "candidates": candidates,
        "meta": result.get("meta") or {},
    }


def _context_token_estimate(messages: list[dict[str, Any]]) -> int:
    serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    return max(1, (len(serialized) + 1) // 2)


def _ensure_context_budget(messages: list[dict[str, Any]]) -> None:
    if _context_token_estimate(messages) > MAX_CONTEXT_TOKENS:
        raise AgentModelError("budget_exceeded")


def _decode_answer_draft_object(content: str) -> dict[str, Any]:
    """Perform one deterministic format repair without inventing business data."""
    normalized_content = content.strip().lstrip("\ufeff")
    if normalized_content.startswith("```") and normalized_content.endswith("```"):
        lines = normalized_content.splitlines()
        if len(lines) >= 3:
            normalized_content = "\n".join(lines[1:-1]).strip()
    try:
        draft = json.loads(normalized_content)
    except (TypeError, ValueError) as error:
        raise AgentModelError("agent_answer_draft_invalid") from error
    if not isinstance(draft, dict):
        raise AgentModelError("agent_answer_draft_invalid")

    allowed_fields = {"response_kind", "service_item_ref", "summary", "claims", "next_actions"}
    repaired = {key: value for key, value in draft.items() if key in allowed_fields}
    claims = repaired.get("claims")
    if isinstance(claims, list):
        repaired["claims"] = [
            {key: value for key, value in claim.items() if key in {"text", "evidence_refs"}}
            if isinstance(claim, dict)
            else claim
            for claim in claims
        ]
    return repaired


def _answer_draft_from_payload(payload: dict[str, Any], allowed_slugs: set[str]) -> dict[str, Any]:
    message = _assistant_message(payload)
    return _answer_draft_from_text(message.get("content"), allowed_slugs)


def _answer_draft_from_text(content: Any, allowed_slugs: set[str]) -> dict[str, Any]:
    if not isinstance(content, str) or not content.strip():
        raise AgentModelError("agent_answer_draft_missing")
    draft = _decode_answer_draft_object(content)
    if draft.get("response_kind") not in {"answer", "clarification", "refusal", "degraded"}:
        raise AgentModelError("agent_answer_draft_invalid")
    service_item_ref = draft.get("service_item_ref")
    if not isinstance(service_item_ref, str) or service_item_ref not in allowed_slugs:
        raise AgentModelError("agent_answer_draft_invalid_reference")
    summary = draft.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 600:
        raise AgentModelError("agent_answer_draft_invalid")
    claims = draft.get("claims")
    if not isinstance(claims, list):
        raise AgentModelError("agent_answer_draft_invalid_reference")
    normalized_claims: list[dict[str, Any]] = []
    evidence_refs: list[str] = []
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(claim.get("text"), str):
            raise AgentModelError("agent_answer_draft_invalid_reference")
        refs = claim.get("evidence_refs")
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in allowed_slugs for ref in refs):
            raise AgentModelError("agent_answer_draft_invalid_reference")
        normalized_refs = list(dict.fromkeys(refs))
        normalized_claims.append({"text": claim["text"].strip(), "evidence_refs": normalized_refs})
        evidence_refs.extend(normalized_refs)
    next_actions = draft.get("next_actions", [])
    if not isinstance(next_actions, list) or any(not isinstance(action, str) or len(action) > 120 for action in next_actions):
        raise AgentModelError("agent_answer_draft_invalid")
    evidence_refs = list(dict.fromkeys(evidence_refs))
    if not evidence_refs:
        evidence_refs = [service_item_ref]
    return {
        "response_kind": draft["response_kind"],
        "service_item_ref": service_item_ref,
        "summary": summary.strip(),
        "claims": normalized_claims,
        "evidence_refs": evidence_refs,
        "next_actions": [action.strip() for action in next_actions if action.strip()][:5],
    }


def run_agent_loop(
    message: str,
    *,
    publication_id: str,
    retrieval_strategy: str,
    config: AgentConfig,
    search_fn: Callable[..., dict[str, Any]],
    retrieval_filters: dict[str, Any] | None = None,
    allow_pre_release: bool = False,
    cancel_event: Event | None = None,
    streaming: bool = False,
) -> dict[str, Any]:
    """Run a bounded model -> tool -> observation -> model loop.

    The model may propose only a read-only service-item search. The deterministic
    runtime remains authoritative for evidence, business rules and side effects.
    """
    if not config.enabled:
        return {
            "mode": "deterministic",
            "provider": "none",
            "model": "deterministic-no-llm-v1",
            "turns": 0,
            "tool_calls": [],
            "retrieval_query": message,
            "retrieval_result": None,
            "answer_draft": None,
            "fallback": False,
            "status": "not_requested",
            "error_code": None,
        }

    system = (
        "你是庆事通的校园事务 Agent。你只能通过已注册的只读检索工具理解用户意图；"
        "不能查询个人数据、登录学校系统、提交真实业务或把虚拟资料说成官方事实。"
        "工具观察返回后，给出简短完成说明；最终事实由后端的证据契约生成。"
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": message},
    ]
    trace_tools: list[dict[str, Any]] = []
    retrieval_result: dict[str, Any] | None = None
    retrieval_query = message
    turns = 0
    active_protocol = config.resolved_api_format
    protocol_attempts: list[str] = []
    fallback_reason: str | None = None
    protocol_lock: str | None = None
    deadline = time.monotonic() + min(config.run_timeout_seconds, DEFAULT_RUN_TIMEOUT_SECONDS)

    def model_call(messages: list[dict[str, Any]], **kwargs: Any) -> ModelTurnResult:
        kwargs["deadline"] = deadline
        if cancel_event is not None:
            kwargs["cancel_event"] = cancel_event
        completion = _chat_completion_stream if streaming else _chat_completion
        request = ModelTurnRequest(
            turn_id=f"turn-{turns + 1}",
            instructions=system,
            input=messages[1:],
            tools=kwargs.pop("tools", []) or [],
            output_schema=kwargs.pop("response_format", {}) or {},
            stream=streaming,
            tool_choice=kwargs.pop("tool_choice", None),
        )

        def chat_transport(transport_config: AgentConfig, model_request: ModelTurnRequest, **transport_kwargs: Any) -> dict[str, Any]:
            chat_messages = [{"role": "system", "content": model_request.instructions}, *model_request.input]
            return completion(
                transport_config,
                chat_messages,
                tools=model_request.tools or None,
                tool_choice=model_request.tool_choice,
                response_format=_chat_output_format(model_request.output_schema),
                **transport_kwargs,
            )

        def responses_transport(transport_config: AgentConfig, model_request: ModelTurnRequest, **transport_kwargs: Any) -> dict[str, Any]:
            return _responses_completion(
                transport_config,
                model_request,
                **transport_kwargs,
            )

        gateway = ModelGateway(
            chat_transport=chat_transport,
            responses_transport=(
                responses_transport
                if config.protocol_policy == "responses_preferred" and protocol_lock != PROTOCOL_CHAT
                else None
            ),
        )
        return gateway.complete(
            config,
            request,
            protocol_lock=protocol_lock,
            allow_fallback=protocol_lock is None,
            **kwargs,
        )

    try:
        _ensure_context_budget(messages)
        _raise_if_cancelled(cancel_event)
        if _remaining_seconds(deadline) <= 0:
            raise AgentModelError("deadline_exceeded")
        first_result = model_call(
            messages,
            tools=[SEARCH_TOOL],
            tool_choice={"type": "function", "function": {"name": TOOL_ID}},
        )
        turns = 1
        active_protocol = first_result.protocol
        protocol_lock = first_result.protocol
        protocol_attempts = list(first_result.protocol_attempts or (first_result.protocol,))
        fallback_reason = first_result.fallback_reason
        _raise_if_cancelled(cancel_event)
        if first_result.provider_error is not None:
            raise AgentModelError(first_result.provider_error.category)
        tool_calls = first_result.tool_calls
        if not tool_calls:
            raise AgentModelError("agent_tool_call_missing")
        if len(tool_calls) != 1:
            raise AgentModelError("tool_calls_multiple_not_allowed")
        call = tool_calls[0]
        if call.get("name") != TOOL_ID:
            raise AgentModelError("agent_tool_not_allowed")
        raw_arguments = call.get("arguments")
        try:
            arguments = json.loads(raw_arguments or "{}")
        except (TypeError, ValueError) as error:
            raise AgentModelError("agent_tool_arguments_invalid") from error
        if not isinstance(arguments, dict) or set(arguments) != {"query"}:
            raise AgentModelError("agent_tool_arguments_invalid")
        raw_query = arguments.get("query")
        if not isinstance(raw_query, str):
            raise AgentModelError("agent_tool_arguments_invalid")
        retrieval_query = raw_query.strip()
        if not retrieval_query or len(retrieval_query) > 2000:
            raise AgentModelError("agent_tool_arguments_invalid")
        if _remaining_seconds(deadline) <= 0:
            raise AgentModelError("deadline_exceeded")
        _raise_if_cancelled(cancel_event)
        search_kwargs: dict[str, Any] = {
            "publication_id": publication_id,
            "strategy": retrieval_strategy,
        }
        if retrieval_filters is not None:
            search_kwargs["filters"] = retrieval_filters
        if allow_pre_release:
            search_kwargs["allow_pre_release"] = True
        retrieval_result = search_fn(retrieval_query, **search_kwargs)
        _raise_if_cancelled(cancel_event)
        compact_result = _compact_search_result(retrieval_result)
        provider_call_id = str(call.get("id") or "tool-call-1")
        runtime_tool_id = f"runtime-tool-{len(trace_tools) + 1}"
        trace_tools.append(
            {
                "tool_call_id": runtime_tool_id,
                "provider_call_id": provider_call_id,
                "tool_id": TOOL_ID,
                "tool_version": "v1",
                "status": "succeeded",
                "input_hash": _input_hash({"query": retrieval_query, "strategy": retrieval_strategy, "filters": retrieval_filters or {}, "allow_pre_release": allow_pre_release}),
                "requested_scope": "service_user",
                "retrieval_scope": "evaluation_pre_release" if allow_pre_release else "published_only",
                "risk_class": "read_only",
                "observation_ref": f"search:{compact_result.get('selected', {}).get('slug') or 'none'}",
            }
        )
        if config.max_turns < 2:
            return {
                "mode": "api",
                "provider": config.provider,
                "base_url": config.resolved_base_url,
                "api_format": active_protocol,
                "model": config.resolved_model,
                "turns": turns,
                "tool_calls": trace_tools,
                "protocol_attempts": protocol_attempts,
                "protocol_fallback": bool(fallback_reason),
                "fallback_reason": fallback_reason,
                "retrieval_query": retrieval_query,
                "retrieval_result": retrieval_result,
                "answer_draft": None,
                "fallback": True,
                "status": "budget_exceeded",
                "error_code": "budget_exceeded",
            }
        messages.append({
            "role": "assistant",
            "content": first_result.text,
            "tool_calls": [{
                "id": provider_call_id,
                "type": "function",
                "function": {"name": call.get("name"), "arguments": raw_arguments},
            }],
        })
        messages.append({"role": "tool", "tool_call_id": provider_call_id, "content": json.dumps(compact_result, ensure_ascii=False)})
        messages.append({"role": "user", "content": ANSWER_DRAFT_INSTRUCTION})
        _ensure_context_budget(messages)
        _raise_if_cancelled(cancel_event)
        if _remaining_seconds(deadline) <= 0:
            raise AgentModelError("deadline_exceeded")
        second_result = model_call(
            messages,
            tools=None,
            tool_choice="none",
            response_format=ANSWER_DRAFT_JSON_SCHEMA,
        )
        turns = 2
        active_protocol = second_result.protocol
        protocol_attempts = list(dict.fromkeys(protocol_attempts + list(second_result.protocol_attempts or (second_result.protocol,))))
        fallback_reason = fallback_reason or second_result.fallback_reason
        _raise_if_cancelled(cancel_event)
        if second_result.provider_error is not None:
            raise AgentModelError(second_result.provider_error.category)
        allowed_slugs = {
            item.get("slug")
            for item in compact_result.get("candidates", [])
            if isinstance(item, dict) and isinstance(item.get("slug"), str)
        }
        selected_slug = compact_result.get("selected", {}).get("slug")
        if isinstance(selected_slug, str):
            allowed_slugs.add(selected_slug)
        answer_draft = _answer_draft_from_text(second_result.text, allowed_slugs)
        if answer_draft["service_item_ref"] != selected_slug:
            raise AgentModelError("agent_answer_draft_invalid_reference")
        return {
            "mode": "api",
            "provider": config.provider,
            "base_url": config.resolved_base_url,
            "api_format": active_protocol,
            "model": config.resolved_model,
            "turns": turns,
            "tool_calls": trace_tools,
            "protocol_attempts": protocol_attempts,
            "protocol_fallback": bool(fallback_reason),
            "fallback_reason": fallback_reason,
            "retrieval_query": retrieval_query,
            "retrieval_result": retrieval_result,
            "answer_draft": answer_draft,
            "fallback": False,
            "status": "completed",
            "error_code": None,
        }
    except AgentModelError as error:
        if str(error) == "cancelled":
            raise
        return {
            "mode": "api",
            "provider": config.provider,
            "base_url": config.resolved_base_url,
            "api_format": active_protocol,
            "model": config.resolved_model or DEFAULT_MODELS.get(config.provider, "unknown"),
            "turns": turns,
            "tool_calls": trace_tools,
            "protocol_attempts": protocol_attempts,
            "protocol_fallback": bool(fallback_reason),
            "fallback_reason": fallback_reason,
            "retrieval_query": retrieval_query,
            "retrieval_result": retrieval_result,
            "answer_draft": None,
            "fallback": True,
            "status": "degraded",
            "error_code": str(error),
        }
    except (KeyError, TypeError, ValueError):
        return {
            "mode": "api",
            "provider": config.provider,
            "base_url": config.resolved_base_url,
            "api_format": active_protocol,
            "model": config.resolved_model or DEFAULT_MODELS.get(config.provider, "unknown"),
            "turns": turns,
            "tool_calls": trace_tools,
            "protocol_attempts": protocol_attempts,
            "protocol_fallback": bool(fallback_reason),
            "fallback_reason": fallback_reason,
            "retrieval_query": retrieval_query,
            "retrieval_result": retrieval_result,
            "answer_draft": None,
            "fallback": True,
            "status": "degraded",
            "error_code": "agent_internal_error",
        }
