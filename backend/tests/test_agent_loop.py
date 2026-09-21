from __future__ import annotations

import json
from threading import Event

import httpx
import pytest

from app.agent import AgentConfig, AgentModelError, run_agent_loop


def test_api_agent_runs_bounded_tool_loop_without_returning_key(monkeypatch):
    calls = []

    def fake_chat(config, messages, *, tools=None, tool_choice=None, response_format=None, deadline=None):
        calls.append({"messages": messages, "tools": tools, "tool_choice": tool_choice})
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "search_service_items",
                                    "arguments": '{"query":"我想申请病假，需要准备什么？"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        return {
            "choices": [{"message": {"role": "assistant", "content": json.dumps({
                "response_kind": "answer",
                "service_item_ref": "leave-application",
                "summary": "依据当前事项资料整理了请假办理信息。",
                "claims": [{"text": "请假事项", "evidence_refs": ["leave-application"]}],
                "next_actions": ["查看事项依据"],
            }, ensure_ascii=False)}}]
        }

    monkeypatch.setattr("app.agent._chat_completion", fake_chat)

    observed = []

    def fake_search(query, *, publication_id, strategy):
        observed.append({"query": query, "publication_id": publication_id, "strategy": strategy})
        return {
            "selected": {"slug": "leave-application", "title": "本科生请假", "source_revision_id": "src-leave-v1"},
            "candidates": [{"slug": "leave-application", "title": "本科生请假", "score": 99, "source_revision_id": "src-leave-v1", "freshness_state": "verified"}],
            "meta": {"stages": ["query_rewrite", "hard_filter"]},
        }

    result = run_agent_loop(
        "我想申请病假，需要准备什么？",
        publication_id="pub-demo-v1",
        retrieval_strategy="c",
        config=AgentConfig(provider="compatible", api_key="test-placeholder-key", model="test-model", base_url="https://relay.example/v1", api_format="chat_completions"),
        search_fn=fake_search,
    )

    assert result["mode"] == "api"
    assert result["status"] == "completed"
    assert result["turns"] == 2
    assert result["tool_calls"][0]["tool_id"] == "search_service_items"
    assert result["answer_draft"]["service_item_ref"] == "leave-application"
    assert result["answer_draft"]["evidence_refs"] == ["leave-application"]
    assert result["retrieval_result"]["selected"]["slug"] == "leave-application"
    assert observed == [{"query": "我想申请病假，需要准备什么？", "publication_id": "pub-demo-v1", "strategy": "c"}]
    assert len(calls) == 2
    assert len(observed) == 1
    assert calls[0]["tool_choice"]["function"]["name"] == "search_service_items"
    assert "response_kind" in calls[1]["messages"][-1]["content"]
    assert "test-placeholder-key" not in json.dumps(result, ensure_ascii=False)


def test_api_agent_accepts_a_fenced_json_answer_draft(monkeypatch):
    calls = []

    def fake_chat(config, messages, *, tools=None, tool_choice=None, response_format=None, deadline=None):
        calls.append(messages)
        if len(calls) == 1:
            return {
                "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "search_service_items", "arguments": '{"query":"查询活动场地"}'},
                }]}}]
            }
        return {"choices": [{"message": {"role": "assistant", "content": "```json\n" + json.dumps({
            "response_kind": "answer",
            "service_item_ref": "venue-application",
            "summary": "已找到活动场地申请事项。",
            "claims": [{"text": "需要先完成材料预检。", "evidence_refs": ["venue-application"]}],
            "next_actions": ["查看事项依据"],
            "provider_debug": "must-be-dropped",
        }, ensure_ascii=False) + "\n```"}}]}

    monkeypatch.setattr("app.agent._chat_completion", fake_chat)
    result = run_agent_loop(
        "查询活动场地",
        publication_id="pub-demo-v1",
        retrieval_strategy="c",
        config=AgentConfig(provider="compatible", api_key="test-key", model="test-model", base_url="https://relay.example/v1", api_format="chat_completions"),
        search_fn=lambda query, **kwargs: {
            "selected": {"slug": "venue-application", "title": "活动场地申请", "source_revision_id": "src-venue-v1"},
            "candidates": [{"slug": "venue-application", "title": "活动场地申请", "score": 99, "source_revision_id": "src-venue-v1", "freshness_state": "verified"}],
            "meta": {},
        },
    )

    assert result["status"] == "completed"
    assert result["answer_draft"]["service_item_ref"] == "venue-application"
    assert "provider_debug" not in result["answer_draft"]


def test_api_agent_runs_responses_primary_tool_loop(monkeypatch):
    requests = []

    def fake_responses(config, request, **kwargs):
        requests.append(request)
        if len(requests) == 1:
            return {
                "status": "completed",
                "output": [{
                    "type": "function_call",
                    "call_id": "responses-call-1",
                    "name": "search_service_items",
                    "arguments": '{"query":"查询活动场地"}',
                }],
            }
        return {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({
                "response_kind": "answer",
                "service_item_ref": "venue-application",
                "summary": "已找到活动场地申请事项。",
                "claims": [{"text": "需要先完成材料预检。", "evidence_refs": ["venue-application"]}],
                "next_actions": ["查看事项依据"],
            }, ensure_ascii=False)}]}],
        }

    monkeypatch.setattr("app.agent._responses_completion", fake_responses)
    monkeypatch.setattr("app.agent._chat_completion", lambda *args, **kwargs: pytest.fail("Responses primary must not call Chat"))
    result = run_agent_loop(
        "我想在示例活动广场办活动",
        publication_id="pub-demo-v1",
        retrieval_strategy="c",
        config=AgentConfig(
            provider="compatible",
            api_key="responses-test-key",
            model="glm-5.2",
            base_url="https://relay.example/v1",
            protocol_policy="responses_preferred",
        ),
        search_fn=lambda query, **kwargs: {
            "selected": {"slug": "venue-application", "title": "活动场地申请", "source_revision_id": "src-venue-v1"},
            "candidates": [{"slug": "venue-application", "title": "活动场地申请", "score": 99, "source_revision_id": "src-venue-v1", "freshness_state": "verified"}],
            "meta": {},
        },
    )

    assert result["status"] == "completed"
    assert result["api_format"] == "responses"
    assert result["answer_draft"]["service_item_ref"] == "venue-application"
    assert len(requests) == 2
    assert requests[0].tools[0]["function"]["name"] == "search_service_items"
    assert any(item.get("role") == "tool" and item.get("tool_call_id") == "responses-call-1" for item in requests[1].input)
    assert result["tool_calls"][0]["tool_call_id"] == "runtime-tool-1"
    assert result["tool_calls"][0]["provider_call_id"] == "responses-call-1"


def test_api_agent_sticks_to_chat_after_responses_capability_fallback(monkeypatch):
    response_calls = []
    chat_calls = []

    def fail_responses(*args, **kwargs):
        response_calls.append(True)
        raise RuntimeError("provider_capability_unsupported")

    def fake_chat(config, messages, *, tools=None, tool_choice=None, response_format=None, deadline=None, cancel_event=None):
        chat_calls.append({"messages": messages, "response_format": response_format})
        if len(chat_calls) == 1:
            return {"choices": [{"message": {"content": None, "tool_calls": [{
                "id": "chat-call-1",
                "type": "function",
                "function": {"name": "search_service_items", "arguments": '{"query":"查询勤工助学"}'},
            }]}}]}
        return {"choices": [{"message": {"content": json.dumps({
            "response_kind": "answer",
            "service_item_ref": "work-study",
            "summary": "已找到勤工助学事项。",
            "claims": [{"text": "请查看事项依据。", "evidence_refs": ["work-study"]}],
            "next_actions": [],
        }, ensure_ascii=False)}}]}

    monkeypatch.setattr("app.agent._responses_completion", fail_responses)
    monkeypatch.setattr("app.agent._chat_completion", fake_chat)
    result = run_agent_loop(
        "我想找勤工助学岗位",
        publication_id="pub-demo-v1",
        retrieval_strategy="c",
        config=AgentConfig(
            provider="compatible",
            api_key="fallback-test-key",
            model="glm-5.2",
            base_url="https://relay.example/v1",
            protocol_policy="responses_preferred",
        ),
        search_fn=lambda query, **kwargs: {
            "selected": {"slug": "work-study", "title": "勤工助学", "source_revision_id": "src-work-v1"},
            "candidates": [{"slug": "work-study", "title": "勤工助学", "score": 99, "source_revision_id": "src-work-v1", "freshness_state": "verified"}],
            "meta": {},
        },
    )

    assert len(response_calls) == 1
    assert len(chat_calls) == 2
    assert result["status"] == "completed"
    assert result["fallback"] is False
    assert result["protocol_fallback"] is True
    assert result["api_format"] == "chat_completions"
    assert result["protocol_attempts"] == ["responses", "chat_completions"]
    assert chat_calls[1]["response_format"] == {"type": "json_object"}


def test_api_agent_does_not_switch_from_responses_after_tool_semantics(monkeypatch):
    response_calls = []
    chat_calls = []

    def responses(config, request, **kwargs):
        response_calls.append(request)
        if len(response_calls) == 1:
            return {"output": [{
                "type": "function_call",
                "call_id": "responses-call-locked",
                "name": "search_service_items",
                "arguments": '{"query":"查询勤工助学"}',
            }]}
        raise RuntimeError("provider_capability_unsupported")

    def chat(*args, **kwargs):
        chat_calls.append(True)
        return {"choices": [{"message": {"content": "must not be used"}}]}

    monkeypatch.setattr("app.agent._responses_completion", responses)
    monkeypatch.setattr("app.agent._chat_completion", chat)
    result = run_agent_loop(
        "我想找勤工助学岗位",
        publication_id="pub-demo-v1",
        retrieval_strategy="c",
        config=AgentConfig(
            provider="compatible",
            api_key="locked-protocol-test-key",
            model="glm-5.2",
            base_url="https://relay.example/v1",
            protocol_policy="responses_preferred",
        ),
        search_fn=lambda query, **kwargs: {
            "selected": {"slug": "work-study", "title": "勤工助学", "source_revision_id": "src-work-v1"},
            "candidates": [{"slug": "work-study", "title": "勤工助学", "score": 99, "source_revision_id": "src-work-v1", "freshness_state": "verified"}],
            "meta": {},
        },
    )

    assert len(response_calls) == 2
    assert chat_calls == []
    assert result["status"] == "degraded"
    assert result["error_code"] == "provider_capability_unsupported"
    assert result["api_format"] == "responses"
    assert result["protocol_attempts"] == ["responses"]
    assert result["protocol_fallback"] is False


def test_api_agent_rejects_multiple_tool_calls_without_running_any(monkeypatch):
    def fake_chat(config, messages, *, tools=None, tool_choice=None, response_format=None, deadline=None):
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "call-1", "function": {"name": "search_service_items", "arguments": '{"query":"选课"}'}},
                        {"id": "call-2", "function": {"name": "search_service_items", "arguments": '{"query":"请假"}'}},
                    ],
                },
            }],
        }

    monkeypatch.setattr("app.agent._chat_completion", fake_chat)
    observed = []
    result = run_agent_loop(
        "我想查校园事项",
        publication_id="pub-demo-v1",
        retrieval_strategy="c",
        config=AgentConfig(provider="compatible", api_key="test-key", model="test-model", base_url="https://relay.example/v1", api_format="chat_completions"),
        search_fn=lambda query, **kwargs: observed.append(query),
    )

    assert result["status"] == "degraded"
    assert result["error_code"] == "tool_calls_multiple_not_allowed"
    assert observed == []


def test_api_agent_checks_cancellation_before_starting_the_tool_or_second_model_call(monkeypatch):
    cancel_event = Event()
    calls = []
    observed = []

    def fake_chat(config, messages, *, tools=None, tool_choice=None, response_format=None, deadline=None, cancel_event=None):
        calls.append(messages)
        cancel_event.set()
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "search_service_items", "arguments": '{"query":"查询"}'},
                            }
                        ],
                    }
                }
            ]
        }

    monkeypatch.setattr("app.agent._chat_completion", fake_chat)

    with pytest.raises(AgentModelError, match="cancelled"):
        run_agent_loop(
            "查询校园事项",
            publication_id="pub-demo-v1",
            retrieval_strategy="c",
            config=AgentConfig(provider="compatible", api_key="test-key", model="test-model", base_url="https://relay.example/v1", api_format="chat_completions"),
            search_fn=lambda query, **kwargs: observed.append(query),
            cancel_event=cancel_event,
        )

    assert len(calls) == 1
    assert observed == []


def test_api_agent_degrades_to_deterministic_query_on_provider_failure(monkeypatch):
    def fail_chat(*args, **kwargs):
        raise AgentModelError("agent_provider_unavailable")

    monkeypatch.setattr("app.agent._chat_completion", fail_chat)
    result = run_agent_loop(
        "我想查勤工助学岗位",
        publication_id="pub-demo-v1",
        retrieval_strategy="c",
        config=AgentConfig(provider="compatible", api_key="test-key", model="test-model", base_url="https://relay.example/v1", api_format="chat_completions"),
        search_fn=lambda query, **kwargs: {},
    )

    assert result["mode"] == "api"
    assert result["status"] == "degraded"
    assert result["fallback"] is True
    assert result["retrieval_query"] == "我想查勤工助学岗位"
    assert result["error_code"] == "agent_provider_unavailable"


def test_agent_config_rejects_credentials_in_base_url_and_derives_relay_endpoint():
    from app.agent import _provider_url

    relay = AgentConfig(provider="compatible", api_key="test-key", model="test-model", base_url="https://relay.example/v1", api_format="chat_completions")
    assert relay.enabled is True
    assert _provider_url(relay) == "https://relay.example/v1/chat/completions"

    unsafe = AgentConfig(provider="compatible", api_key="test-key", model="test-model", base_url="https://user:pass@relay.example/v1", api_format="chat_completions")
    assert unsafe.enabled is False


def test_provider_defaults_to_chat_completions_endpoint():
    from app.agent import _provider_url

    config = AgentConfig(provider="openrouter", api_key="test-key", model="test-model")
    assert config.resolved_base_url == "https://openrouter.ai/api/v1"
    assert config.resolved_api_format == "chat_completions"
    assert _provider_url(config) == "https://openrouter.ai/api/v1/chat/completions"


def test_chat_adapter_posts_payload_to_configured_endpoint(monkeypatch):
    from app import agent

    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    real_client = agent.httpx.Client
    transport = httpx.MockTransport(handler)

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(agent.httpx, "Client", client_factory)
    config = agent.AgentConfig(
        provider="compatible",
        api_key="chat-test-key",
        model="test-model",
        base_url="https://relay.example/v1",
        api_format="chat_completions",
        chat_reasoning_effort="none",
    )
    payload = agent._chat_completion(
        config,
        [{"role": "user", "content": "选课什么时候开始？"}],
        tools=[agent.SEARCH_TOOL],
        tool_choice={"type": "function", "function": {"name": agent.TOOL_ID}},
    )

    assert captured["url"] == "https://relay.example/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer chat-test-key"
    assert captured["body"]["model"] == "test-model"
    assert captured["body"]["messages"][0]["content"] == "选课什么时候开始？"
    assert captured["body"]["reasoning_effort"] == "none"
    assert captured["body"]["parallel_tool_calls"] is False
    assert "chat-test-key" not in json.dumps(captured["body"], ensure_ascii=False)
    assert payload["choices"][0]["message"]["content"] == "ok"


def test_chat_stream_adapter_aggregates_provider_events_without_exposing_raw_payload(monkeypatch):
    from app import agent

    def handler(request):
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["reasoning_effort"] == "none"
        assert body["parallel_tool_calls"] is False
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b'data: {"id":"chat-1","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call-1","type":"function","function":{"name":"search_service_items","arguments":"{\\"query\\":\\""}}]},"finish_reason":null}]}\n\n'
                b'data: {"id":"chat-1","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"type":"function","function":{"arguments":"\\u573a\\u5730\\"}"}}]},"finish_reason":null}]}\n\n'
                b'data: {"id":"chat-1","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n'
                b'data: [DONE]\n\n'
            ),
        )

    real_client = agent.httpx.Client
    transport = httpx.MockTransport(handler)

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(agent.httpx, "Client", client_factory)
    config = agent.AgentConfig(
        provider="compatible",
        api_key="stream-test-key",
        model="test-model",
        base_url="https://relay.example/v1",
        api_format="responses",
        protocol_policy="responses_preferred",
        chat_reasoning_effort="none",
    )

    payload = agent._chat_completion_stream(
        config,
        [{"role": "user", "content": "查询场地"}],
        tools=[agent.SEARCH_TOOL],
        tool_choice={"type": "function", "function": {"name": agent.TOOL_ID}},
    )

    message = payload["choices"][0]["message"]
    assert len(message["tool_calls"]) == 1
    assert message["tool_calls"][0]["function"] == {
        "name": "search_service_items",
        "arguments": '{"query":"场地"}',
    }
    assert payload["choices"][0]["finish_reason"] == "tool_calls"
    assert "stream-test-key" not in json.dumps(payload, ensure_ascii=False)


def test_agent_loop_uses_stream_adapter_for_streaming_runs(monkeypatch):
    from app import agent

    calls = []

    def fake_stream(config, messages, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": agent.TOOL_ID, "arguments": '{"query":"请假"}'},
                        }],
                    },
                }],
            }
        return {
            "choices": [{"message": {"role": "assistant", "content": json.dumps({
                "response_kind": "answer",
                "service_item_ref": "leave-application",
                "summary": "依据事项资料整理。",
                "claims": [{"text": "请假事项", "evidence_refs": ["leave-application"]}],
                "next_actions": [],
            }, ensure_ascii=False)}}]
        }

    def fail_buffered(*args, **kwargs):
        raise AssertionError("streaming run must not use buffered adapter")

    monkeypatch.setattr(agent, "_chat_completion_stream", fake_stream)
    monkeypatch.setattr(agent, "_chat_completion", fail_buffered)

    result = agent.run_agent_loop(
        "我想申请病假",
        publication_id="pub-demo-v1",
        retrieval_strategy="c",
        config=agent.AgentConfig(provider="compatible", api_key="test-key", model="test-model", base_url="https://relay.example/v1", api_format="chat_completions"),
        search_fn=lambda query, **kwargs: {
            "selected": {"slug": "leave-application"},
            "candidates": [{"slug": "leave-application", "title": "本科生请假", "score": 1, "source_revision_id": "src-leave-v1", "freshness_state": "verified"}],
            "meta": {},
        },
        streaming=True,
    )

    assert result["status"] == "completed"
    assert len(calls) == 2


def test_agent_loop_uses_responses_stream_adapter_for_streaming_runs(monkeypatch):
    from app import agent

    calls = []

    def fake_responses(config, request, **kwargs):
        assert request.stream is True
        calls.append(request)
        if len(calls) == 1:
            return {"output": [{
                "type": "function_call",
                "call_id": "responses-stream-call-1",
                "name": agent.TOOL_ID,
                "arguments": '{"query":"请假"}',
            }]}
        return {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({
            "response_kind": "answer",
            "service_item_ref": "leave-application",
            "summary": "依据事项资料整理。",
            "claims": [{"text": "请假事项", "evidence_refs": ["leave-application"]}],
            "next_actions": [],
        }, ensure_ascii=False)}]}]}

    def fail_chat(*args, **kwargs):
        raise AssertionError("Responses streaming must not use Chat")

    monkeypatch.setattr(agent, "_responses_completion", fake_responses)
    monkeypatch.setattr(agent, "_chat_completion", fail_chat)
    monkeypatch.setattr(agent, "_chat_completion_stream", fail_chat)

    result = agent.run_agent_loop(
        "我想申请病假",
        publication_id="pub-demo-v1",
        retrieval_strategy="c",
        config=agent.AgentConfig(
            provider="compatible",
            api_key="responses-stream-test-key",
            model="glm-5.2",
            base_url="https://relay.example/v1",
            protocol_policy="responses_preferred",
        ),
        search_fn=lambda query, **kwargs: {
            "selected": {"slug": "leave-application"},
            "candidates": [{"slug": "leave-application", "title": "本科生请假", "score": 1, "source_revision_id": "src-leave-v1", "freshness_state": "verified"}],
            "meta": {},
        },
        streaming=True,
    )

    assert result["status"] == "completed"
    assert result["api_format"] == "responses"
    assert len(calls) == 2


def test_responses_stream_does_not_return_incomplete_output(monkeypatch):
    from app import agent

    monkeypatch.setattr(
        agent,
        "iter_provider_http_events_with_retry",
        lambda *args, **kwargs: iter([
            type("Event", (), {"kind": "text_delta", "text": '{"response_kind":"answer"}', "tool_call_id": None, "tool_name": None, "tool_arguments": None, "usage": None, "finish_reason": None})(),
            type("Event", (), {"kind": "upstream_end", "text": None, "tool_call_id": None, "tool_name": None, "tool_arguments": None, "usage": None, "finish_reason": "incomplete"})(),
        ]),
    )
    config = agent.AgentConfig(
        provider="compatible",
        api_key="responses-incomplete-test-key",
        model="glm-5.2",
        base_url="https://relay.example/v1",
        protocol_policy="responses_preferred",
    )

    with pytest.raises(agent.AgentModelError, match="provider_stream_incomplete"):
        agent._responses_completion(
            config,
            agent.ModelTurnRequest("turn-incomplete", "回答", [{"role": "user", "content": "查询"}], [], {}, stream=True),
        )


def test_streaming_agent_falls_back_to_chat_only_before_responses_events(monkeypatch):
    responses_calls = []
    chat_calls = []

    def fail_responses(config, request, **kwargs):
        responses_calls.append(request)
        raise RuntimeError("provider_capability_unsupported")

    def fake_chat_stream(config, messages, **kwargs):
        chat_calls.append(messages)
        if len(chat_calls) == 1:
            return {"choices": [{"message": {"content": None, "tool_calls": [{
                "id": "chat-stream-call-1",
                "type": "function",
                "function": {"name": "search_service_items", "arguments": '{"query":"请假"}'},
            }]}}]}
        return {"choices": [{"message": {"content": json.dumps({
            "response_kind": "answer",
            "service_item_ref": "leave-application",
            "summary": "依据事项资料整理。",
            "claims": [{"text": "请假事项", "evidence_refs": ["leave-application"]}],
            "next_actions": [],
        }, ensure_ascii=False)}}]}

    monkeypatch.setattr("app.agent._responses_completion", fail_responses)
    monkeypatch.setattr("app.agent._chat_completion_stream", fake_chat_stream)
    monkeypatch.setattr("app.agent._chat_completion", lambda *args, **kwargs: pytest.fail("streaming fallback must not use buffered Chat"))

    result = run_agent_loop(
        "我想申请病假",
        publication_id="pub-demo-v1",
        retrieval_strategy="c",
        config=AgentConfig(
            provider="compatible",
            api_key="stream-fallback-test-key",
            model="glm-5.2",
            base_url="https://relay.example/v1",
            protocol_policy="responses_preferred",
        ),
        search_fn=lambda query, **kwargs: {
            "selected": {"slug": "leave-application"},
            "candidates": [{"slug": "leave-application", "title": "本科生请假", "score": 1, "source_revision_id": "src-leave-v1", "freshness_state": "verified"}],
            "meta": {},
        },
        streaming=True,
    )

    assert result["status"] == "completed"
    assert len(responses_calls) == 1
    assert len(chat_calls) == 2
    assert result["api_format"] == "chat_completions"
    assert result["protocol_attempts"] == ["responses", "chat_completions"]
