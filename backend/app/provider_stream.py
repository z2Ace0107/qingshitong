"""Normalize provider SSE streams at the Runtime boundary.

This module deliberately exposes only safe, protocol-neutral candidate events.
Provider payloads stay inside the adapter and are never stored on an event.
"""

from __future__ import annotations

import codecs
import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from threading import Event
from typing import Any, Literal

import httpx


DEFAULT_MAX_EVENT_BYTES = 64 * 1024
SUPPORTED_PROTOCOLS = {"responses", "chat_completions"}
MAX_CONNECT_RETRIES = 1
CONNECT_RETRYABLE_ERRORS = frozenset({
    "provider_connect_timeout",
    "provider_connect_error",
})


class ProviderStreamError(ValueError):
    """A provider stream cannot be safely normalized."""

    def __init__(self, code: str, *, response_started: bool = False):
        self.code = code
        self.response_started = response_started
        super().__init__(code)


@dataclass(frozen=True)
class SSEFrame:
    """One completed SSE frame, including a comment-only heartbeat."""

    event: str | None = None
    event_id: str | None = None
    data: str = ""
    comment: str | None = None


@dataclass(frozen=True)
class ProviderStreamCapabilities:
    """Capabilities that Runtime must inspect before selecting a stream path."""

    supports_streaming: bool
    supports_tool_events: bool
    cancellation: Literal["best_effort", "unsupported"]
    fallback_mode: Literal["stream", "buffered"]


@dataclass(frozen=True)
class ProviderStreamEvent:
    """A safe candidate event consumed by Runtime, never by the student UI."""

    kind: Literal[
        "heartbeat",
        "lifecycle",
        "text_delta",
        "tool_call_delta",
        "tool_call_done",
        "usage",
        "upstream_end",
        "stream_closed",
        "error",
    ]
    provider: str
    protocol: Literal["responses", "chat_completions"]
    event_id: str | None = None
    sequence: int | None = None
    text: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments: str | None = None
    finish_reason: str | None = None
    usage: dict[str, int] | None = None
    error_code: str | None = None
    tool_call_index: int | None = None


def response_indicates_capability_unsupported(response: httpx.Response, protocol: str) -> bool:
    """Classify only explicit protocol endpoint/capability failures."""
    if protocol != "responses" or response.status_code not in {400, 404, 405, 501}:
        return False
    try:
        payload = response.json()
    except (TypeError, ValueError):
        payload = None
    fragments: list[str] = []
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            for key in ("code", "type", "message", "param"):
                value = error.get(key)
                if isinstance(value, str):
                    fragments.append(value.lower())
        for key in ("code", "type", "message"):
            value = payload.get(key)
            if isinstance(value, str):
                fragments.append(value.lower())
    codes = {
        "responses_not_supported",
        "responses_unsupported",
        "unsupported_responses",
        "responses_endpoint_not_found",
        "responses_endpoint_unsupported",
        "responses_not_implemented",
        "unsupported_endpoint",
        "endpoint_not_found",
    }
    if any(fragment in codes for fragment in fragments):
        return True
    text = " ".join(fragments)
    phrases = (
        "responses endpoint is not supported",
        "responses endpoint unsupported",
        "responses endpoint not found",
        "responses endpoint not implemented",
        "responses api is not supported",
        "responses api unsupported",
        "responses api not implemented",
        "unknown responses endpoint",
        "unsupported responses endpoint",
        "unsupported endpoint /responses",
        "route /responses not found",
        "endpoint /responses not found",
    )
    return any(phrase in text for phrase in phrases)


def iter_provider_http_events(
    client: httpx.Client,
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    provider: str,
    protocol: str,
    cancel_event: Event | None = None,
    timeout: float | None = None,
    max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES,
) -> Iterator[ProviderStreamEvent]:
    """Open one configured SSE request and normalize it at the adapter boundary.

    Runtime owns retry, budgets and tool permissions. This function only maps
    transport failures and delegates payload parsing to ``iter_provider_events``.
    """

    if cancel_event is not None and cancel_event.is_set():
        raise ProviderStreamError("provider_cancelled")

    response_started = False
    try:
        with client.stream("POST", url, headers=headers, json=payload, timeout=timeout) as response:
            response_started = True
            if response.status_code in {401, 403}:
                raise ProviderStreamError("provider_unauthorized")
            if response.status_code == 429:
                raise ProviderStreamError("provider_rate_limited")
            if response.status_code in {408, 425, 500, 502, 503, 504}:
                raise ProviderStreamError("provider_unavailable")
            if response.status_code >= 400:
                response.read()
                if response_indicates_capability_unsupported(response, protocol):
                    raise ProviderStreamError("provider_capability_unsupported")
                raise ProviderStreamError("provider_http_error")
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type != "text/event-stream":
                raise ProviderStreamError("provider_content_type_invalid")

            def chunks() -> Iterator[bytes]:
                for chunk in response.iter_bytes():
                    if cancel_event is not None and cancel_event.is_set():
                        raise ProviderStreamError("provider_cancelled")
                    yield chunk

            yield from iter_provider_events(
                chunks(),
                provider=provider,
                protocol=protocol,
                max_event_bytes=max_event_bytes,
            )
    except ProviderStreamError as error:
        # Parser failures happen after the response headers were accepted. A
        # caller must not replay a request after any provider bytes could have
        # been observed, so preserve that fact on the normalized error.
        if response_started and not error.response_started:
            raise ProviderStreamError(error.code, response_started=True) from error
        raise
    except httpx.TimeoutException as error:
        raise ProviderStreamError("provider_timeout_after_headers" if response_started else "provider_connect_timeout") from error
    except httpx.RequestError as error:
        raise ProviderStreamError("provider_stream_read_error" if response_started else "provider_connect_error") from error


def should_retry_provider_stream(error: ProviderStreamError, *, attempt: int, max_retries: int = MAX_CONNECT_RETRIES) -> bool:
    """Allow at most one retry, and only when no response has started."""

    if max_retries < 0 or max_retries > MAX_CONNECT_RETRIES:
        raise ValueError("provider_stream_max_retries_out_of_range")
    return attempt < max_retries and not error.response_started and error.code in CONNECT_RETRYABLE_ERRORS


def iter_provider_http_events_with_retry(
    client: httpx.Client,
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    provider: str,
    protocol: str,
    cancel_event: Event | None = None,
    timeout: float | None = None,
    max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES,
    max_retries: int = MAX_CONNECT_RETRIES,
) -> Iterator[ProviderStreamEvent]:
    """Retry only a connection failure that produced no response or event."""

    attempt = 0
    while True:
        try:
            yield from iter_provider_http_events(
                client,
                url=url,
                headers=headers,
                payload=payload,
                provider=provider,
                protocol=protocol,
                cancel_event=cancel_event,
                timeout=timeout,
                max_event_bytes=max_event_bytes,
            )
            return
        except ProviderStreamError as error:
            if not should_retry_provider_stream(error, attempt=attempt, max_retries=max_retries):
                raise
            attempt += 1


def provider_stream_capabilities(protocol: str) -> ProviderStreamCapabilities:
    """Return an explicit stream capability, including buffered fallback."""

    if protocol in SUPPORTED_PROTOCOLS:
        return ProviderStreamCapabilities(
            supports_streaming=True,
            supports_tool_events=True,
            cancellation="best_effort",
            fallback_mode="stream",
        )
    return ProviderStreamCapabilities(
        supports_streaming=False,
        supports_tool_events=False,
        cancellation="unsupported",
        fallback_mode="buffered",
    )


def _decode_chunks(chunks: Iterable[bytes | str]) -> Iterator[str]:
    decoder = codecs.getincrementaldecoder("utf-8")()
    for chunk in chunks:
        if isinstance(chunk, str):
            yield chunk
            continue
        if not isinstance(chunk, bytes):
            raise ProviderStreamError("provider_chunk_invalid")
        try:
            decoded = decoder.decode(chunk, final=False)
        except UnicodeDecodeError as error:
            raise ProviderStreamError("provider_invalid_utf8") from error
        if decoded:
            yield decoded
    try:
        tail = decoder.decode(b"", final=True)
    except UnicodeDecodeError as error:
        raise ProviderStreamError("provider_invalid_utf8") from error
    if tail:
        yield tail


def _iter_lines(chunks: Iterable[bytes | str]) -> Iterator[str]:
    buffer = ""
    for text in _decode_chunks(chunks):
        buffer += text
        while buffer:
            match = re.search(r"\r\n|\r|\n", buffer)
            if match is None:
                break
            separator = match.group(0)
            if separator == "\r" and match.end() == len(buffer):
                break
            line = buffer[: match.start()]
            buffer = buffer[match.end() :]
            yield line
    if buffer:
        yield buffer


def iter_sse_frames(
    chunks: Iterable[bytes | str],
    *,
    max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES,
) -> Iterator[SSEFrame]:
    """Parse fragmented SSE input without exposing raw network chunks."""

    if max_event_bytes < 1:
        raise ValueError("max_event_bytes must be positive")

    event: str | None = None
    event_id: str | None = None
    data_lines: list[str] = []
    comments: list[str] = []
    frame_bytes = 0

    def reset() -> None:
        nonlocal event, event_id, data_lines, comments, frame_bytes
        event = None
        event_id = None
        data_lines = []
        comments = []
        frame_bytes = 0

    for line in _iter_lines(chunks):
        frame_bytes += len(line.encode("utf-8")) + 1
        if frame_bytes > max_event_bytes:
            raise ProviderStreamError("provider_event_too_large")

        if line == "":
            if data_lines or event is not None or event_id is not None or comments:
                yield SSEFrame(
                    event=event,
                    event_id=event_id,
                    data="\n".join(data_lines),
                    comment=" | ".join(comments) if comments else None,
                )
            reset()
            continue

        if line.startswith(":"):
            comments.append(line[1:].lstrip(" "))
            continue

        field, separator, value = line.partition(":")
        if not separator:
            value = ""
        elif value.startswith(" "):
            value = value[1:]

        if field == "event":
            event = value
        elif field == "id":
            event_id = value
        elif field == "data":
            data_lines.append(value)
        # SSE's retry and unknown fields are transport metadata. They are
        # intentionally ignored after the frame-size guard above.

    if data_lines or event is not None or event_id is not None or comments:
        raise ProviderStreamError("provider_stream_incomplete")


def _json_object(frame: SSEFrame) -> dict[str, Any]:
    try:
        payload = json.loads(frame.data)
    except (TypeError, ValueError) as error:
        raise ProviderStreamError("provider_invalid_json") from error
    if not isinstance(payload, dict):
        raise ProviderStreamError("provider_event_not_object")
    return payload


def _sequence(payload: dict[str, Any]) -> int | None:
    if "sequence_number" not in payload:
        return None
    value = payload["sequence_number"]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProviderStreamError("provider_sequence_invalid")
    return value


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _error_code(payload: dict[str, Any], fallback: str) -> str:
    nested = payload.get("error")
    value = nested.get("code") if isinstance(nested, dict) else None
    value = value or payload.get("code") or fallback
    return str(value)[:120]


def _event_id(frame: SSEFrame, payload: dict[str, Any], *, response_id: bool = False) -> str | None:
    if frame.event_id:
        return frame.event_id
    if response_id:
        response = payload.get("response")
        if isinstance(response, dict) and isinstance(response.get("id"), str):
            return response["id"]
    return _string_or_none(payload.get("id"))


def _responses_event(
    provider: str,
    frame: SSEFrame,
    *,
    item_call_ids: dict[str, str],
) -> ProviderStreamEvent:
    if frame.comment is not None and not frame.data:
        return ProviderStreamEvent("heartbeat", provider, "responses", event_id=frame.event_id)
    payload = _json_object(frame)
    event_name = frame.event or _string_or_none(payload.get("type"))
    if not event_name:
        raise ProviderStreamError("provider_event_type_missing")
    sequence = _sequence(payload)
    event_id = _event_id(frame, payload, response_id=event_name in {"response.completed", "response.incomplete"})

    if event_name == "response.output_text.delta":
        delta = payload.get("delta")
        if not isinstance(delta, str):
            raise ProviderStreamError("provider_text_delta_invalid")
        return ProviderStreamEvent("text_delta", provider, "responses", event_id=event_id, sequence=sequence, text=delta)

    if event_name == "response.function_call_arguments.delta":
        arguments = payload.get("delta")
        if not isinstance(arguments, str):
            raise ProviderStreamError("provider_tool_delta_invalid")
        return ProviderStreamEvent(
            "tool_call_delta",
            provider,
            "responses",
            event_id=event_id,
            sequence=sequence,
            tool_call_id=(
                _string_or_none(payload.get("call_id"))
                or item_call_ids.get(_string_or_none(payload.get("item_id")) or "")
                or _string_or_none(payload.get("item_id"))
            ),
            tool_arguments=arguments,
        )

    if event_name == "response.function_call_arguments.done":
        return ProviderStreamEvent(
            "tool_call_done",
            provider,
            "responses",
            event_id=event_id,
            sequence=sequence,
            tool_call_id=(
                _string_or_none(payload.get("call_id"))
                or item_call_ids.get(_string_or_none(payload.get("item_id")) or "")
                or _string_or_none(payload.get("item_id"))
            ),
        )

    if event_name in {"response.output_item.added", "response.output_item.done"}:
        item = payload.get("item")
        if isinstance(item, dict) and item.get("type") == "function_call":
            arguments = item.get("arguments") if event_name == "response.output_item.added" else None
            if arguments is not None and not isinstance(arguments, str):
                raise ProviderStreamError("provider_tool_delta_invalid")
            item_id = _string_or_none(item.get("id"))
            call_id = _string_or_none(item.get("call_id")) or item_id
            if item_id and call_id:
                item_call_ids[item_id] = call_id
            return ProviderStreamEvent(
                "lifecycle",
                provider,
                "responses",
                event_id=event_id,
                sequence=sequence,
                tool_call_id=call_id,
                tool_name=_string_or_none(item.get("name")),
                tool_arguments=arguments or "",
            )
        return ProviderStreamEvent("lifecycle", provider, "responses", event_id=event_id, sequence=sequence)

    if event_name == "response.completed":
        response = payload.get("response")
        usage = response.get("usage") if isinstance(response, dict) else payload.get("usage")
        return ProviderStreamEvent(
            "upstream_end",
            provider,
            "responses",
            event_id=event_id,
            sequence=sequence,
            finish_reason="completed",
            usage=_usage(usage) if usage is not None else None,
        )

    if event_name == "response.incomplete":
        response = payload.get("response")
        details = response.get("incomplete_details") if isinstance(response, dict) else None
        reason = details.get("reason") if isinstance(details, dict) else None
        usage = response.get("usage") if isinstance(response, dict) else payload.get("usage")
        return ProviderStreamEvent(
            "upstream_end",
            provider,
            "responses",
            event_id=event_id,
            sequence=sequence,
            finish_reason=_string_or_none(reason) or "incomplete",
            usage=_usage(usage) if usage is not None else None,
        )

    if event_name in {"response.failed", "error"}:
        return ProviderStreamEvent(
            "error",
            provider,
            "responses",
            event_id=event_id,
            sequence=sequence,
            error_code=_error_code(payload, event_name.replace(".", "_")),
        )

    known_lifecycle = {
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.output_item.done",
        "response.content_part.added",
        "response.content_part.done",
        "response.output_text.done",
        "response.reasoning_summary_part.added",
        "response.reasoning_summary_part.done",
        "response.reasoning_summary_text.delta",
        "response.reasoning_summary_text.done",
    }
    if event_name in known_lifecycle:
        return ProviderStreamEvent("lifecycle", provider, "responses", event_id=event_id, sequence=sequence)
    raise ProviderStreamError("provider_event_unknown")


def _usage(payload: dict[str, Any]) -> dict[str, int]:
    if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
        payload = payload["usage"]
    if not isinstance(payload, dict):
        raise ProviderStreamError("provider_usage_invalid")
    normalized: dict[str, int] = {}
    aliases = {
        "prompt_tokens": ("prompt_tokens", "input_tokens"),
        "completion_tokens": ("completion_tokens", "output_tokens"),
        "total_tokens": ("total_tokens",),
    }
    for target, keys in aliases.items():
        value = next((payload.get(key) for key in keys if key in payload), None)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            normalized[target] = value
    return normalized


def _chat_event(provider: str, frame: SSEFrame) -> ProviderStreamEvent:
    if frame.comment is not None and not frame.data:
        return ProviderStreamEvent("heartbeat", provider, "chat_completions", event_id=frame.event_id)
    if frame.data == "[DONE]":
        return ProviderStreamEvent("stream_closed", provider, "chat_completions", event_id=frame.event_id)
    payload = _json_object(frame)
    event_id = _event_id(frame, payload)
    if isinstance(payload.get("error"), dict):
        return ProviderStreamEvent(
            "error",
            provider,
            "chat_completions",
            event_id=event_id,
            error_code=_error_code(payload, "provider_stream_error"),
        )

    choices = payload.get("choices")
    if not isinstance(choices, list):
        raise ProviderStreamError("provider_choices_invalid")
    if not choices:
        if "usage" in payload:
            return ProviderStreamEvent("usage", provider, "chat_completions", event_id=event_id, usage=_usage(payload))
        raise ProviderStreamError("provider_choices_empty")
    if len(choices) != 1:
        raise ProviderStreamError("provider_multiple_choices_not_supported")

    choice = choices[0]
    if not isinstance(choice, dict):
        raise ProviderStreamError("provider_choice_invalid")
    delta = choice.get("delta")
    if not isinstance(delta, dict):
        raise ProviderStreamError("provider_delta_invalid")
    finish_reason = choice.get("finish_reason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise ProviderStreamError("provider_finish_reason_invalid")
    if finish_reason == "error":
        return ProviderStreamEvent("error", provider, "chat_completions", event_id=event_id, error_code="provider_stream_error")

    content = delta.get("content")
    if isinstance(content, str) and content:
        return ProviderStreamEvent("text_delta", provider, "chat_completions", event_id=event_id, text=content)
    if content is not None and not isinstance(content, str):
        raise ProviderStreamError("provider_text_delta_invalid")

    tool_calls = delta.get("tool_calls")
    if tool_calls is not None:
        if not isinstance(tool_calls, list) or len(tool_calls) != 1 or not isinstance(tool_calls[0], dict):
            raise ProviderStreamError("provider_tool_delta_invalid")
        tool_call = tool_calls[0]
        tool_call_index = tool_call.get("index")
        if (
            tool_call_index is not None
            and (
                not isinstance(tool_call_index, int)
                or isinstance(tool_call_index, bool)
                or tool_call_index < 0
            )
        ):
            raise ProviderStreamError("provider_tool_delta_invalid")
        function = tool_call.get("function") or {}
        if not isinstance(function, dict):
            raise ProviderStreamError("provider_tool_delta_invalid")
        arguments = function.get("arguments")
        if arguments is not None and not isinstance(arguments, str):
            raise ProviderStreamError("provider_tool_delta_invalid")
        return ProviderStreamEvent(
            "tool_call_delta",
            provider,
            "chat_completions",
            event_id=event_id,
            tool_call_id=_string_or_none(tool_call.get("id")),
            tool_name=_string_or_none(function.get("name")),
            tool_arguments=arguments or "",
            tool_call_index=tool_call_index,
        )

    if finish_reason:
        return ProviderStreamEvent("upstream_end", provider, "chat_completions", event_id=event_id, finish_reason=finish_reason)
    return ProviderStreamEvent("lifecycle", provider, "chat_completions", event_id=event_id)


def iter_provider_events(
    chunks: Iterable[bytes | str],
    *,
    provider: str,
    protocol: str,
    max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES,
) -> Iterator[ProviderStreamEvent]:
    """Yield normalized events and reject invalid ordered Responses streams."""

    if protocol not in SUPPORTED_PROTOCOLS:
        raise ProviderStreamError("provider_protocol_unsupported")
    last_sequence: int | None = None
    terminal_kind: str | None = None
    item_call_ids: dict[str, str] = {}
    for frame in iter_sse_frames(chunks, max_event_bytes=max_event_bytes):
        event = (
            _responses_event(provider, frame, item_call_ids=item_call_ids)
            if protocol == "responses"
            else _chat_event(provider, frame)
        )
        if terminal_kind is not None:
            if terminal_kind == "upstream_end" and event.kind == "stream_closed":
                terminal_kind = "stream_closed"
            else:
                raise ProviderStreamError("provider_terminal_duplicate" if event.kind in {"upstream_end", "stream_closed", "error"} else "provider_event_after_terminal")
        elif event.kind in {"upstream_end", "error"}:
            terminal_kind = event.kind
        elif event.kind == "stream_closed":
            terminal_kind = event.kind
        if event.sequence is not None:
            if last_sequence is not None and event.sequence <= last_sequence:
                raise ProviderStreamError("provider_sequence_invalid")
            last_sequence = event.sequence
        yield event
    if terminal_kind is None:
        raise ProviderStreamError("provider_stream_incomplete")
