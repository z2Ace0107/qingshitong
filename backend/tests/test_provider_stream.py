from __future__ import annotations

import json
import httpx

import pytest

from app.provider_stream import (
    ProviderStreamError,
    iter_provider_events,
    iter_provider_http_events,
    iter_provider_http_events_with_retry,
    iter_sse_frames,
    provider_stream_capabilities,
)


def _sse(*blocks: str) -> list[str]:
    return [block + "\n\n" for block in blocks]


def test_sse_parser_reassembles_fragmented_multiline_data_and_comment_heartbeat():
    chunks = [
        b": OPENROUTER PRO",
        b"CESSING\r\n\r\nevent: message\r\ndata: {\"text\":\r\n",
        b'data: \"hello\"}\r\n\r\n',
    ]

    frames = list(iter_sse_frames(chunks))

    assert frames[0].comment == "OPENROUTER PROCESSING"
    assert frames[0].data == ""
    assert frames[1].event == "message"
    assert frames[1].data == '{"text":\n"hello"}'
    assert frames[1].event_id is None


def test_chat_completions_events_normalize_without_exposing_provider_payload():
    chunks = _sse(
        ": keep-alive",
        'data: {"id":"chat-1","choices":[{"index":0,"delta":{"content":"你好"},"finish_reason":null}]}',
        'data: {"id":"chat-1","choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}',
        'data: {"id":"chat-1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    )

    events = list(iter_provider_events(chunks, provider="openrouter", protocol="chat_completions"))

    assert [event.kind for event in events] == [
        "heartbeat",
        "text_delta",
        "usage",
        "upstream_end",
        "stream_closed",
    ]
    assert events[1].text == "你好"
    assert events[2].usage == {"prompt_tokens": 3, "completion_tokens": 2}
    assert events[3].finish_reason == "stop"
    assert all(not hasattr(event, "raw") for event in events)
    assert "choices" not in json.dumps(events[1].__dict__, ensure_ascii=False)
    assert "payload" not in events[1].__dict__


def test_chat_stream_empty_finish_reason_is_not_a_terminal_event():
    chunks = _sse(
        'data: {"id":"chat-2","choices":[{"index":0,"delta":{"role":"assistant","content":"好"},"finish_reason":""}]}',
        'data: {"id":"chat-2","choices":[{"index":0,"delta":{"content":"的"},"finish_reason":""}]}',
        'data: {"id":"chat-2","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    )

    events = list(iter_provider_events(chunks, provider="compatible", protocol="chat_completions"))

    assert [event.kind for event in events] == ["text_delta", "text_delta", "upstream_end", "stream_closed"]


def test_chat_tool_call_fragments_preserve_index_when_later_chunks_omit_id():
    chunks = _sse(
        'data: {"id":"chat-3","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call-1","type":"function","function":{"name":"search_service_items","arguments":"{\\"query\\":\\""}}]},"finish_reason":null}]}',
        'data: {"id":"chat-3","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"type":"function","function":{"arguments":"\\u573a\\u5730\\"}"}}]},"finish_reason":null}]}',
        'data: {"id":"chat-3","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    )

    events = list(iter_provider_events(chunks, provider="compatible", protocol="chat_completions"))
    tool_events = [event for event in events if event.kind == "tool_call_delta"]

    assert [event.tool_call_id for event in tool_events] == ["call-1", None]
    assert [event.tool_call_index for event in tool_events] == [0, 0]


def test_responses_events_preserve_safe_sequence_and_tool_delta_fields():
    chunks = _sse(
        'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","sequence_number":1,"item_id":"msg-1","delta":"办事条件"}',
        'event: response.function_call_arguments.delta\ndata: {"type":"response.function_call_arguments.delta","sequence_number":2,"item_id":"call-1","delta":"{\\"query\\":\\"场地\\"}"}',
        'event: response.completed\ndata: {"type":"response.completed","sequence_number":3,"response":{"id":"resp-1"}}',
    )

    events = list(iter_provider_events(chunks, provider="compatible", protocol="responses"))

    assert [event.kind for event in events] == ["text_delta", "tool_call_delta", "upstream_end"]
    assert events[0].sequence == 1
    assert events[0].text == "办事条件"
    assert events[1].tool_call_id == "call-1"
    assert events[1].tool_arguments == '{"query":"场地"}'
    assert events[2].sequence == 3
    assert events[2].event_id == "resp-1"


def test_responses_function_call_lifecycle_preserves_name_and_completion_usage():
    chunks = _sse(
        'event: response.output_item.added\ndata: {"type":"response.output_item.added","sequence_number":1,"item":{"type":"function_call","id":"fc-1","call_id":"call-1","name":"search_service_items","arguments":""}}',
        'event: response.function_call_arguments.delta\ndata: {"type":"response.function_call_arguments.delta","sequence_number":2,"item_id":"fc-1","delta":"{\\"query\\":\\"场地\\"}"}',
        'event: response.function_call_arguments.done\ndata: {"type":"response.function_call_arguments.done","sequence_number":3,"call_id":"call-1"}',
        'event: response.completed\ndata: {"type":"response.completed","sequence_number":4,"response":{"id":"resp-1","usage":{"input_tokens":3,"output_tokens":2,"total_tokens":5}}}',
    )

    events = list(iter_provider_events(chunks, provider="compatible", protocol="responses"))

    assert [event.kind for event in events] == ["lifecycle", "tool_call_delta", "tool_call_done", "upstream_end"]
    assert events[0].tool_call_id == "call-1"
    assert events[0].tool_name == "search_service_items"
    assert events[-1].usage == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}


def test_responses_stream_maps_item_id_to_provider_call_id_for_tool_arguments():
    chunks = _sse(
        'event: response.output_item.added\ndata: {"type":"response.output_item.added","sequence_number":1,"item":{"type":"function_call","id":"item-1","call_id":"call-1","name":"search_service_items","arguments":""}}',
        'event: response.function_call_arguments.delta\ndata: {"type":"response.function_call_arguments.delta","sequence_number":2,"item_id":"item-1","delta":"{\\"query\\":\\"场地\\"}"}',
        'event: response.function_call_arguments.done\ndata: {"type":"response.function_call_arguments.done","sequence_number":3,"item_id":"item-1"}',
        'event: response.completed\ndata: {"type":"response.completed","sequence_number":4,"response":{"id":"resp-1"}}',
    )

    events = list(iter_provider_events(chunks, provider="compatible", protocol="responses"))

    tool_events = [event for event in events if event.kind in {"lifecycle", "tool_call_delta", "tool_call_done"}]
    assert {event.tool_call_id for event in tool_events} == {"call-1"}


def test_responses_reasoning_summary_events_are_internal_lifecycle_events():
    chunks = _sse(
        'event: response.created\ndata: {"type":"response.created","sequence_number":1,"response":{"id":"resp-2","status":"in_progress"}}',
        'event: response.reasoning_summary_text.delta\ndata: {"type":"response.reasoning_summary_text.delta","sequence_number":2,"delta":"内部摘要"}',
        'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","sequence_number":3,"delta":"OK"}',
        'event: response.reasoning_summary_part.done\ndata: {"type":"response.reasoning_summary_part.done","sequence_number":4}',
        'event: response.completed\ndata: {"type":"response.completed","sequence_number":5,"response":{"id":"resp-2"}}',
    )

    events = list(iter_provider_events(chunks, provider="compatible", protocol="responses"))

    assert [event.kind for event in events] == ["lifecycle", "lifecycle", "text_delta", "lifecycle", "upstream_end"]
    assert all(event.text != "内部摘要" for event in events)


def test_provider_stream_rejects_bad_json_and_out_of_order_sequences():
    with pytest.raises(ProviderStreamError, match="provider_invalid_json"):
        list(iter_provider_events(_sse("data: {bad"), provider="compatible", protocol="responses"))

    chunks = _sse(
        'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","sequence_number":2,"delta":"后"}',
        'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","sequence_number":2,"delta":"前"}',
    )
    with pytest.raises(ProviderStreamError, match="provider_sequence_invalid"):
        list(iter_provider_events(chunks, provider="compatible", protocol="responses"))


def test_provider_stream_rejects_duplicate_responses_terminal():
    chunks = _sse(
        'event: response.completed\ndata: {"type":"response.completed","sequence_number":1,"response":{"id":"resp-1"}}',
        'event: response.completed\ndata: {"type":"response.completed","sequence_number":2,"response":{"id":"resp-1"}}',
    )

    with pytest.raises(ProviderStreamError, match="provider_terminal_duplicate"):
        list(iter_provider_events(chunks, provider="compatible", protocol="responses"))


def test_provider_stream_rejects_duplicate_chat_stream_closed_marker():
    chunks = _sse(
        "data: [DONE]",
        "data: [DONE]",
    )

    with pytest.raises(ProviderStreamError, match="provider_terminal_duplicate"):
        list(iter_provider_events(chunks, provider="compatible", protocol="chat_completions"))


def test_http_provider_adapter_normalizes_a_mock_sse_response():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b'data: {"id":"chat-1","choices":[{"index":0,"delta":{"content":"\\u4f60\\u597d"},"finish_reason":null}]}\n\n'
                b'data: {"id":"chat-1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                b"data: [DONE]\n\n"
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        events = list(
            iter_provider_http_events(
                client,
                url="https://relay.example/v1/chat/completions",
                headers={"Authorization": "Bearer test-secret"},
                payload={"model": "test-model", "stream": True},
                provider="compatible",
                protocol="chat_completions",
            )
        )

    assert captured == {
        "url": "https://relay.example/v1/chat/completions",
        "body": {"model": "test-model", "stream": True},
    }
    assert [event.kind for event in events] == ["text_delta", "upstream_end", "stream_closed"]
    assert events[0].text == "你好"


def test_http_provider_adapter_maps_http_failures_without_returning_body():
    def handler(request):
        return httpx.Response(503, content=b"provider secret response")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderStreamError, match="provider_unavailable") as error:
            list(
                iter_provider_http_events(
                    client,
                    url="https://relay.example/v1/responses",
                    headers={},
                    payload={"model": "test-model", "stream": True},
                    provider="compatible",
                    protocol="responses",
                )
            )

    assert "provider secret response" not in str(error.value)


def test_http_provider_stream_classifies_only_explicit_responses_capability_failure():
    responses = [
        httpx.Response(404, json={"error": {"code": "responses_endpoint_not_found", "message": "Responses endpoint not found"}}),
        httpx.Response(404, json={"error": {"code": "model_not_found", "message": "The model does not exist"}}),
    ]

    def handler(request):
        return responses.pop(0)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderStreamError, match="provider_capability_unsupported"):
            list(iter_provider_http_events(
                client,
                url="https://relay.example/v1/responses",
                headers={},
                payload={"model": "test-model", "stream": True},
                provider="compatible",
                protocol="responses",
            ))
        with pytest.raises(ProviderStreamError, match="provider_http_error"):
            list(iter_provider_http_events(
                client,
                url="https://relay.example/v1/responses",
                headers={},
                payload={"model": "test-model", "stream": True},
                provider="compatible",
                protocol="responses",
            ))


def test_http_provider_stream_reads_unbuffered_error_body_before_classifying():
    class ErrorBody(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"error":{"code":"responses_endpoint_not_found","message":"Responses endpoint not found"}}'

        def close(self):
            return None

    def handler(request):
        return httpx.Response(
            404,
            headers={"content-type": "application/json"},
            stream=ErrorBody(),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderStreamError, match="provider_capability_unsupported"):
            list(iter_provider_http_events(
                client,
                url="https://relay.example/v1/responses",
                headers={},
                payload={"model": "test-model", "stream": True},
                provider="compatible",
                protocol="responses",
            ))


def test_provider_stream_rejects_oversized_event():
    oversized = "data: " + ("x" * (64 * 1024))

    with pytest.raises(ProviderStreamError, match="provider_event_too_large"):
        list(iter_sse_frames(_sse(oversized)))


def test_unsupported_protocol_declares_buffered_fallback_instead_of_streaming():
    capabilities = provider_stream_capabilities("unknown_protocol")

    assert capabilities.supports_streaming is False
    assert capabilities.fallback_mode == "buffered"
    assert capabilities.supports_tool_events is False


def test_provider_http_stream_retries_once_only_before_response_headers():
    calls = {"count": 0}

    def handler(request):
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.ConnectTimeout("connection unavailable")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b'data: {"id":"chat-1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                b"data: [DONE]\n\n"
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        events = list(
            iter_provider_http_events_with_retry(
                client,
                url="https://relay.example/v1/chat/completions",
                headers={},
                payload={"model": "test-model", "stream": True},
                provider="compatible",
                protocol="chat_completions",
            )
        )

    assert calls["count"] == 2
    assert [event.kind for event in events] == ["upstream_end", "stream_closed"]


def test_provider_http_stream_never_replays_after_headers_or_first_event():
    calls = {"count": 0}

    class ReadFailsAfterHeaders(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"id":"chat-1","choices":[{"index":0,"delta":{"content":"\\u5148"},"finish_reason":null}]}\n\n'
            raise httpx.ReadTimeout("stream interrupted")

        def close(self):
            return None

    def handler(request):
        calls["count"] += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ReadFailsAfterHeaders(),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderStreamError, match="provider_timeout_after_headers"):
            list(
                iter_provider_http_events_with_retry(
                    client,
                    url="https://relay.example/v1/chat/completions",
                    headers={},
                    payload={"model": "test-model", "stream": True},
                    provider="compatible",
                    protocol="chat_completions",
                )
            )

    assert calls["count"] == 1
