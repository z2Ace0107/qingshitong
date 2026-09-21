from __future__ import annotations

from app.model_gateway import (
    CapabilityMatrix,
    ModelTurnRequest,
    ModelTurnResult,
    ModelGateway,
    normalize_responses_payload,
    responses_tool_definitions,
    NormalizedStreamEvent,
    ProtocolPolicy,
)


def test_protocol_policy_keeps_responses_primary_and_chat_as_fallback():
    policy = ProtocolPolicy.responses_preferred()

    assert policy.primary == "responses"
    assert policy.fallback == "chat_completions"
    assert policy.can_fallback(before_semantic_event=True, reason="provider_capability_unsupported")
    assert not policy.can_fallback(before_semantic_event=False, reason="provider_capability_unsupported")
    assert not policy.can_fallback(before_semantic_event=True, reason="provider_invalid_response")


def test_capability_matrix_distinguishes_declared_from_observed_without_secrets():
    matrix = CapabilityMatrix.unverified().with_declaration("responses", "declared")
    matrix = matrix.with_observation("responses", "passed")

    assert matrix["responses"] == {"declared": "declared", "observed": "passed"}
    assert matrix["chat_completions"]["observed"] == "unverified"
    assert matrix["protocol_fallback"]["observed"] == "unverified"
    assert "api_key" not in matrix


def test_normalized_contract_is_protocol_neutral():
    request = ModelTurnRequest(
        turn_id="turn-1",
        instructions="只生成结构化回答草稿",
        input=[{"role": "user", "content": "查询场地申请"}],
        tools=[],
        output_schema={"type": "object"},
        stream=True,
    )
    result = ModelTurnResult(protocol="responses", structured_payload={"response_kind": "answer"})
    event = NormalizedStreamEvent(kind="completed", sequence=1, payload={"valid": True})

    assert request.stream is True
    assert result.protocol == "responses"
    assert event.kind == "completed"


def test_runtime_can_call_a_gateway_boundary_without_knowing_provider_transport():
    calls = []

    request = ModelTurnRequest(
        turn_id="turn-1",
        instructions="系统约束",
        input=[{"role": "user", "content": "查询"}],
        tools=[],
        output_schema={},
    )

    def transport(config, received_request, **kwargs):
        calls.append((config, received_request, kwargs))
        return {"choices": [{"message": {"content": "ok"}}]}

    gateway = ModelGateway(chat_transport=transport)
    payload = gateway.complete("config", request, stream=False)

    assert isinstance(payload, ModelTurnResult)
    assert payload.protocol == "chat_completions"
    assert payload.text == "ok"
    assert calls == [("config", request, {"stream": False})]


def test_gateway_normalizes_chat_tool_calls_without_returning_provider_shape():
    def transport(config, messages, **kwargs):
        return {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "provider-call",
                        "type": "function",
                        "function": {
                            "name": "search_service_items",
                            "arguments": '{"query":"场地申请"}',
                        },
                    }],
                },
            }],
        }

    result = ModelGateway(chat_transport=transport).complete("config", [])

    assert result.tool_calls == [{
        "id": "provider-call",
        "name": "search_service_items",
        "arguments": '{"query":"场地申请"}',
    }]
    assert "choices" not in result.__dict__


def test_gateway_normalizes_transport_errors_without_leaking_provider_details():
    def transport(config, messages, **kwargs):
        raise RuntimeError("secret endpoint and authorization must not escape")

    result = ModelGateway(chat_transport=transport).complete("config", [])

    assert result.provider_error is not None
    assert result.provider_error.category == "provider_transport_error"
    assert result.provider_error.retryable is True
    assert "authorization" not in str(result.provider_error)


def test_gateway_can_record_an_observed_capability_for_later_smoke_evidence():
    gateway = ModelGateway(chat_transport=lambda config, request: {})

    matrix = gateway.observe_capability("responses", "passed")

    assert matrix["responses"]["observed"] == "passed"


def test_responses_output_items_normalize_function_call_and_text():
    payload = {
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "id": "fc-item",
                "call_id": "call-responses",
                "name": "search_service_items",
                "arguments": '{"query":"活动场地"}',
            },
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "已找到事项"}],
            },
        ],
        "usage": {"output_tokens": 12},
    }

    result = normalize_responses_payload(payload)

    assert result.protocol == "responses"
    assert result.tool_calls == [{
        "id": "call-responses",
        "name": "search_service_items",
        "arguments": '{"query":"活动场地"}',
    }]
    assert result.text == "已找到事项"
    assert result.usage_summary == {"output_tokens": 12}


def test_responses_gateway_uses_primary_transport_and_translates_tool_schema():
    requests = []

    def responses_transport(config, request, **kwargs):
        requests.append(request)
        return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]}

    request = ModelTurnRequest(
        turn_id="turn-responses",
        instructions="回答业务问题",
        input=[{"role": "user", "content": "查询事项"}],
        tools=[{"type": "function", "function": {"name": "search_service_items", "description": "检索", "parameters": {"type": "object"}}}],
        output_schema={},
    )
    result = ModelGateway(chat_transport=lambda *args, **kwargs: {}, responses_transport=responses_transport).complete("config", request)

    assert result.protocol == "responses"
    assert result.text == "ok"
    assert requests == [request]
    assert responses_tool_definitions(request.tools) == [{
        "type": "function",
        "name": "search_service_items",
        "description": "检索",
        "parameters": {"type": "object"},
    }]


def test_gateway_falls_back_to_chat_only_for_capability_unsupported_before_semantics():
    calls = []

    def responses_transport(config, request, **kwargs):
        calls.append("responses")
        raise RuntimeError("provider_capability_unsupported")

    def chat_transport(config, request, **kwargs):
        calls.append("chat")
        return {"choices": [{"message": {"content": "fallback answer"}}]}

    class ResponsesConfig:
        resolved_api_format = "responses"

    result = ModelGateway(
        chat_transport=chat_transport,
        responses_transport=responses_transport,
    ).complete(ResponsesConfig(), ModelTurnRequest("turn-1", "", [], [], {}))

    assert calls == ["responses", "chat"]
    assert result.protocol == "chat_completions"
    assert result.text == "fallback answer"
    assert result.protocol_attempts == ("responses", "chat_completions")
    assert result.fallback_reason == "provider_capability_unsupported"


def test_gateway_does_not_fallback_after_run_protocol_is_locked():
    calls = []

    def responses_transport(config, request, **kwargs):
        calls.append("responses")
        raise RuntimeError("provider_capability_unsupported")

    def chat_transport(config, request, **kwargs):
        calls.append("chat")
        return {"choices": [{"message": {"content": "must not be used"}}]}

    class ResponsesConfig:
        resolved_api_format = "responses"

    result = ModelGateway(
        chat_transport=chat_transport,
        responses_transport=responses_transport,
    ).complete(
        ResponsesConfig(),
        ModelTurnRequest("turn-2", "", [], [], {}),
        protocol_lock="responses",
        allow_fallback=False,
    )

    assert calls == ["responses"]
    assert result.protocol == "responses"
    assert result.provider_error is not None
    assert result.provider_error.category == "provider_capability_unsupported"
    assert result.fallback_reason is None


def test_gateway_does_not_fallback_for_non_capability_provider_error():
    calls = []

    def responses_transport(config, request, **kwargs):
        calls.append("responses")
        raise RuntimeError("provider_http_error")

    def chat_transport(config, request, **kwargs):
        calls.append("chat")
        return {"choices": [{"message": {"content": "must not be used"}}]}

    class ResponsesConfig:
        resolved_api_format = "responses"

    result = ModelGateway(
        chat_transport=chat_transport,
        responses_transport=responses_transport,
    ).complete(ResponsesConfig(), ModelTurnRequest("turn-1", "", [], [], {}))

    assert calls == ["responses"]
    assert result.protocol == "responses"
    assert result.provider_error is not None
    assert result.provider_error.category == "provider_http_error"
    assert result.fallback_reason is None
