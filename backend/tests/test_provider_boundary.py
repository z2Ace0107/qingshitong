from __future__ import annotations

import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db
from app import agent
from app import main as main_module
from app.agent import AgentConfig, AgentModelError, ProviderConfigurationError, load_provider_profile
from app.model_gateway import ModelTurnRequest
from app.main import app
from app.runtime import get_run


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "provider-boundary.sqlite3")
    for name in (
        "QST_LLM_PROVIDER",
        "QST_LLM_BASE_URL",
        "QST_LLM_MODEL",
        "QST_LLM_API_KEY",
        "QST_LLM_API_KEY_FILE",
        "QST_ALLOW_PRIVATE_PROVIDER_ENDPOINT",
        "QST_LLM_DECLARED_CAPABILITIES",
        "QST_LLM_RESPONSES_REQUEST_VARIANT",
        "QST_LLM_CHAT_REASONING_EFFORT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QST_APP_ENV", "development")
    with TestClient(app) as test_client:
        yield test_client


def test_student_query_rejects_provider_and_secret_overrides(client: TestClient):
    secret = "request-level-secret-must-not-be-accepted"
    response = client.post(
        "/api/query",
        json={
            "message": "我想查勤工助学岗位",
            "provider": "compatible",
            "model": "attacker-model",
            "base_url": "https://attacker.example/v1",
            "api_key": secret,
            "retrieval_strategy": "a",
            "actor_type": "demo_provider",
        },
    )

    assert response.status_code == 422
    assert secret not in json.dumps(response.json(), ensure_ascii=False)


def test_secret_file_wins_over_environment_fallback(tmp_path, monkeypatch):
    secret_file = tmp_path / "qst-llm-api-key"
    secret_file.write_text("file-secret\n", encoding="utf-8")
    monkeypatch.setenv("QST_APP_ENV", "development")
    monkeypatch.setenv("QST_LLM_PROVIDER", "compatible")
    monkeypatch.setenv("QST_LLM_BASE_URL", "https://relay.example/v1")
    monkeypatch.setenv("QST_LLM_MODEL", "server-model")
    monkeypatch.setenv("QST_LLM_API_KEY", "environment-secret")
    monkeypatch.setenv("QST_LLM_API_KEY_FILE", str(secret_file))

    profile = load_provider_profile()

    assert profile.api_key == "file-secret"
    assert profile.secret_source == "secret_file"
    assert profile.protocol == "responses_preferred"
    assert profile.to_agent_config().resolved_api_format == "responses"
    assert profile.capabilities["responses"]["observed"] == "unverified"
    assert profile.to_health_payload()["configured"] is True
    assert "file-secret" not in json.dumps(profile.to_health_payload(), ensure_ascii=False)


def test_provider_profile_keeps_declared_capabilities_separate_from_observed(monkeypatch):
    monkeypatch.setenv("QST_LLM_PROVIDER", "compatible")
    monkeypatch.setenv("QST_LLM_BASE_URL", "https://relay.example/v1")
    monkeypatch.setenv("QST_LLM_MODEL", "server-model")
    monkeypatch.setenv("QST_LLM_API_KEY", "server-secret")
    monkeypatch.setenv("QST_LLM_DECLARED_CAPABILITIES", '{"responses":"declared","protocol_fallback":"declared"}')

    profile = load_provider_profile()
    observed = profile.with_capability_observation("responses", "passed")

    assert profile.capabilities["responses"] == {"declared": "declared", "observed": "unverified"}
    assert observed.capabilities["responses"] == {"declared": "declared", "observed": "passed"}
    assert profile.capabilities["protocol_fallback"]["declared"] == "declared"


def test_provider_profile_accepts_explicit_responses_request_variant(monkeypatch):
    monkeypatch.setenv("QST_LLM_PROVIDER", "compatible")
    monkeypatch.setenv("QST_LLM_BASE_URL", "https://relay.example/v1")
    monkeypatch.setenv("QST_LLM_MODEL", "server-model")
    monkeypatch.setenv("QST_LLM_API_KEY", "server-secret")
    monkeypatch.setenv("QST_LLM_RESPONSES_REQUEST_VARIANT", "message_input_max_tokens")

    profile = load_provider_profile()

    assert profile.responses_request_variant == "message_input_max_tokens"
    assert profile.to_agent_config().responses_request_variant == "message_input_max_tokens"


def test_provider_profile_accepts_chat_reasoning_effort(monkeypatch):
    monkeypatch.setenv("QST_LLM_PROVIDER", "compatible")
    monkeypatch.setenv("QST_LLM_BASE_URL", "https://relay.example/v1")
    monkeypatch.setenv("QST_LLM_MODEL", "server-model")
    monkeypatch.setenv("QST_LLM_API_KEY", "server-secret")
    monkeypatch.setenv("QST_LLM_CHAT_REASONING_EFFORT", "none")

    profile = load_provider_profile()

    assert profile.chat_reasoning_effort == "none"
    assert profile.to_agent_config().chat_reasoning_effort == "none"


def test_provider_profile_rejects_unknown_chat_reasoning_effort(monkeypatch):
    monkeypatch.setenv("QST_LLM_PROVIDER", "compatible")
    monkeypatch.setenv("QST_LLM_BASE_URL", "https://relay.example/v1")
    monkeypatch.setenv("QST_LLM_MODEL", "server-model")
    monkeypatch.setenv("QST_LLM_API_KEY", "server-secret")
    monkeypatch.setenv("QST_LLM_CHAT_REASONING_EFFORT", "extreme")

    profile = load_provider_profile()

    assert profile.configured is False
    assert profile.config_error == "provider_chat_reasoning_effort_invalid"


def test_provider_profile_rejects_unknown_responses_request_variant(monkeypatch):
    monkeypatch.setenv("QST_LLM_PROVIDER", "compatible")
    monkeypatch.setenv("QST_LLM_BASE_URL", "https://relay.example/v1")
    monkeypatch.setenv("QST_LLM_MODEL", "server-model")
    monkeypatch.setenv("QST_LLM_API_KEY", "server-secret")
    monkeypatch.setenv("QST_LLM_RESPONSES_REQUEST_VARIANT", "unknown")

    profile = load_provider_profile()

    assert profile.configured is False
    assert profile.config_error == "provider_responses_request_variant_invalid"


def test_production_secret_conflict_rejects_startup(tmp_path, monkeypatch):
    secret_file = tmp_path / "qst-llm-api-key"
    secret_file.write_text("file-secret", encoding="utf-8")
    monkeypatch.setenv("QST_APP_ENV", "production")
    monkeypatch.setenv("QST_LLM_PROVIDER", "compatible")
    monkeypatch.setenv("QST_LLM_BASE_URL", "https://relay.example/v1")
    monkeypatch.setenv("QST_LLM_MODEL", "server-model")
    monkeypatch.setenv("QST_LLM_API_KEY", "environment-secret")
    monkeypatch.setenv("QST_LLM_API_KEY_FILE", str(secret_file))

    with pytest.raises(ProviderConfigurationError, match="secret_conflict"):
        load_provider_profile()


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:9000/v1",
        "https://localhost/v1",
        "https://169.254.169.254/latest",
        "https://10.0.0.8/v1",
    ],
)
def test_provider_profile_rejects_private_or_metadata_endpoint_without_explicit_opt_in(monkeypatch, base_url):
    monkeypatch.setenv("QST_APP_ENV", "development")
    monkeypatch.setenv("QST_LLM_PROVIDER", "compatible")
    monkeypatch.setenv("QST_LLM_BASE_URL", base_url)
    monkeypatch.setenv("QST_LLM_MODEL", "server-model")
    monkeypatch.setenv("QST_LLM_API_KEY", "endpoint-boundary-secret")
    monkeypatch.delenv("QST_ALLOW_PRIVATE_PROVIDER_ENDPOINT", raising=False)

    profile = load_provider_profile()

    assert profile.config_error == "provider_endpoint_not_allowed"
    assert profile.configured is False


def test_public_demo_rejects_arbitrary_provider_endpoint(monkeypatch):
    monkeypatch.setenv("QST_APP_ENV", "public_demo")
    monkeypatch.setenv("QST_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("QST_LLM_BASE_URL", "https://relay.example/api/v1")
    monkeypatch.setenv("QST_LLM_MODEL", "server-model")
    monkeypatch.setenv("QST_LLM_API_KEY", "public-demo-secret")

    profile = load_provider_profile()

    assert profile.config_error == "provider_endpoint_not_allowed"
    assert profile.configured is False


def test_public_demo_rejects_compatible_provider_without_fixed_host(monkeypatch):
    monkeypatch.setenv("QST_APP_ENV", "public_demo")
    monkeypatch.setenv("QST_LLM_PROVIDER", "compatible")
    monkeypatch.setenv("QST_LLM_BASE_URL", "https://relay.example/api/v1")
    monkeypatch.setenv("QST_LLM_MODEL", "server-model")
    monkeypatch.setenv("QST_LLM_API_KEY", "public-demo-secret")

    profile = load_provider_profile()

    assert profile.config_error == "provider_endpoint_not_allowed"
    assert profile.configured is False


def test_ready_health_is_degraded_without_optional_provider(client: TestClient):
    response = client.get("/api/health/ready")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["database"] == "ok"
    assert payload["publication"] == "ok"
    assert payload["retrieval"] == "degraded"
    assert payload["provider_status"] == "not_configured"
    assert payload["deterministic_core"] == "ok"
    assert "llm_answer" in payload["degraded_capabilities"]
    assert payload["checked_at"].endswith("+00:00")
    assert payload["capabilities"]["service_catalog"] == "available"
    assert payload["capabilities"]["assisted_response"] == "limited"
    assert "api_key" not in json.dumps(payload, ensure_ascii=False).lower()


def test_ready_requires_deterministic_core_path(client: TestClient, monkeypatch):
    monkeypatch.setattr(
        main_module,
        "_deterministic_core_health",
        lambda publication_id: {"status": "unavailable", "reason_code": "deterministic_core_unavailable"},
    )

    response = client.get("/api/health/ready")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert payload["deterministic_core"] == "unavailable"


def test_provider_profile_is_snapshotted_at_startup(client: TestClient, monkeypatch):
    monkeypatch.setattr(
        main_module,
        "load_provider_profile",
        lambda: pytest.fail("provider profile must not be reloaded after startup"),
    )

    health = client.get("/api/health/ready")
    query = client.post("/api/query", json={"message": "我想查勤工助学岗位"})

    assert health.status_code == 200
    assert health.json()["provider_status"] == "not_configured"
    assert query.status_code == 200


def test_dependencies_health_is_structured_and_never_echoes_secret(client: TestClient, monkeypatch):
    secret = "health-endpoint-secret"
    monkeypatch.setenv("QST_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("QST_LLM_API_KEY", secret)
    main_module.app.state.provider_profile = load_provider_profile()

    response = client.get("/api/health/dependencies")

    assert response.status_code == 200
    payload = response.json()
    assert {"database", "publication", "retrieval_index", "provider", "worker", "trace", "checked_at"}.issubset(payload)
    assert payload["database"]["status"] == "ok"
    assert payload["publication"]["status"] == "ok"
    assert payload["retrieval_index"]["status"] == "degraded"
    assert payload["provider"]["status"] == "configured"
    assert payload["publication"]["binding"]["id"].startswith("sha256:")
    assert payload["retrieval_index"]["binding"]["embedding_profile"] == "bge-base-zh-v1.5"
    assert payload["trace"]["status"] == "ok"
    assert payload["worker"] == {"status": "not_configured", "reason_code": "r0_single_service"}
    assert secret not in json.dumps(payload, ensure_ascii=False)
    assert "authorization" not in json.dumps(payload, ensure_ascii=False).lower()
    assert "https://" not in json.dumps(payload, ensure_ascii=False)


def test_dependencies_health_reports_trace_degradation(client: TestClient, monkeypatch):
    monkeypatch.setattr(
        main_module,
        "_trace_health",
        lambda: {"status": "degraded", "reason_code": "trace_schema_missing"},
    )

    response = client.get("/api/health/dependencies")

    assert response.status_code == 200
    assert response.json()["trace"] == {"status": "degraded", "reason_code": "trace_schema_missing"}


def test_ready_returns_not_ready_when_publication_is_missing(client: TestClient, monkeypatch):
    monkeypatch.setattr(main_module, "get_current_publication", lambda: {})

    response = client.get("/api/health/ready")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert payload["database"] == "ok"
    assert payload["publication"] == "unavailable"
    assert payload["retrieval"] == "unavailable"
    assert payload["provider_status"] == "not_configured"


def test_ready_returns_not_ready_when_database_is_unavailable(client: TestClient, monkeypatch):
    def unavailable_database():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(main_module, "connect", unavailable_database)

    response = client.get("/api/health/ready")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert payload["database"] == "unavailable"
    assert payload["publication"] == "unavailable"
    assert payload["retrieval"] == "unavailable"
    assert "database is locked" not in json.dumps(payload, ensure_ascii=False)


def test_ready_returns_not_ready_when_retrieval_index_is_unavailable(client: TestClient, monkeypatch):
    monkeypatch.setattr(
        main_module,
        "get_retrieval_health",
        lambda publication_id=None: {
            "status": "unavailable",
            "publication_id": publication_id,
            "keyword_index": {"status": "missing"},
            "dense_profile": {"status": "unavailable"},
            "dense_index": {"status": "missing"},
        },
    )

    response = client.get("/api/health/ready")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert payload["database"] == "ok"
    assert payload["publication"] == "ok"
    assert payload["retrieval"] == "unavailable"


def test_configured_openrouter_uses_server_profile_for_responses(client: TestClient, monkeypatch):
    secret = "server-only-provider-secret"
    monkeypatch.setenv("QST_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("QST_LLM_BASE_URL", "https://openrouter.example/api/v1")
    monkeypatch.setenv("QST_LLM_MODEL", "nex-agi/nex-n2.5-mini:free")
    monkeypatch.setenv("QST_LLM_API_KEY", secret)
    main_module.app.state.provider_profile = load_provider_profile()
    calls = []

    def fake_responses(config, request, **kwargs):
        calls.append({"config": config, "request": request})
        if len(calls) == 1:
            return {
                "status": "completed",
                "output": [{
                    "type": "function_call",
                    "call_id": "call-provider-boundary",
                    "name": "search_service_items",
                    "arguments": '{"query":"我想查勤工助学岗位"}',
                }],
            }
        return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({
            "response_kind": "answer",
            "service_item_ref": "work-study",
            "summary": "已根据当前事项资料整理勤工助学信息。",
            "claims": [{"text": "勤工助学事项", "evidence_refs": ["work-study"]}],
            "next_actions": ["查看事项依据"],
        }, ensure_ascii=False)}]}]}

    monkeypatch.setattr(agent, "_responses_completion", fake_responses)

    health = client.get("/api/health/ready")
    response = client.post("/api/query", json={"message": "我想查勤工助学岗位"})

    assert health.status_code == 200
    assert health.json()["status"] == "degraded"
    assert health.json()["capabilities"]["service_catalog"] == "available"
    assert health.json()["capabilities"]["assisted_response"] == "limited"
    assert response.status_code == 200
    assert "agent" not in response.json()["response"]
    assert calls[0]["config"].provider == "openrouter"
    assert calls[0]["config"].resolved_api_format == "responses"
    assert set(response.json()) == {"response", "feedback_ref"}
    assert response.json()["response"]["claims"][0]["text"] == "勤工助学事项"
    assert response.json()["response"]["next_actions"] == ["查看事项依据"]
    assert secret not in json.dumps(response.json(), ensure_ascii=False)


def test_configured_provider_failure_degrades_at_api_boundary_without_secret(client: TestClient, monkeypatch):
    secret = "provider-failure-secret"
    monkeypatch.setenv("QST_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("QST_LLM_BASE_URL", "https://openrouter.example/api/v1")
    monkeypatch.setenv("QST_LLM_MODEL", "nex-agi/nex-n2.5-mini:free")
    monkeypatch.setenv("QST_LLM_API_KEY", secret)
    main_module.app.state.provider_profile = load_provider_profile()

    def fail_responses(*args, **kwargs):
        raise AgentModelError("provider_timeout")

    monkeypatch.setattr(agent, "_responses_completion", fail_responses)

    response = client.post("/api/query", json={"message": "我想查勤工助学岗位"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["response"]["degradation"]["state"] == "limited"
    assert secret not in json.dumps(payload, ensure_ascii=False)
    with db.connect() as connection:
        run = connection.execute("SELECT error_code FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
    assert run["error_code"] == "provider_timeout"


def test_startup_marks_incomplete_runs_interrupted_after_process_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "restart.sqlite3")
    for name in (
        "QST_LLM_PROVIDER",
        "QST_LLM_BASE_URL",
        "QST_LLM_MODEL",
        "QST_LLM_API_KEY",
        "QST_LLM_API_KEY_FILE",
        "QST_ALLOW_PRIVATE_PROVIDER_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QST_APP_ENV", "development")
    db.init_db()
    with db.connect() as connection:
        connection.execute(
            """INSERT INTO runs
            (id, feedback_ref, session_id, channel, input_text, status, intent, context_manifest, steps, response, created_at, completed_at, error_code)
            VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, NULL, NULL)""",
            (
                "run-before-restart",
                "feedback-before-restart",
                "session-before-restart",
                "standalone_web",
                "查询场地申请",
                db.json_dumps({"selected": "venue-application"}),
                db.json_dumps({"knowledge_publication_id": "pub-demo-2026-09-15"}),
                db.json_dumps([]),
                db.json_dumps({}),
                "2026-09-19T00:00:00+00:00",
            ),
        )

    with TestClient(app):
        recovered = get_run("run-before-restart")

    assert recovered["status"] == "interrupted"
    assert recovered["error_code"] == "process_restart"
    assert recovered["completed_at"]


def test_chat_completion_adapter_targets_compatible_chat_endpoint(monkeypatch):
    captured = {}
    transport = httpx.MockTransport(
        lambda request: (
            captured.update({
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": json.loads(request.content),
            })
            or httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
        )
    )
    real_client = agent.httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(agent.httpx, "Client", client_factory)
    config = AgentConfig(
        provider="openrouter",
        api_key="chat-completions-test-secret",
        model="nex-agi/nex-n2.5-mini:free",
        base_url="https://relay.example/v1",
        api_format="chat_completions",
    )

    payload = agent._chat_completion(
        config,
        [{"role": "user", "content": "查勤工助学岗位"}],
        tools=[agent.SEARCH_TOOL],
        tool_choice={"type": "function", "function": {"name": agent.TOOL_ID}},
    )

    assert captured["url"] == "https://relay.example/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer chat-completions-test-secret"
    assert captured["body"]["model"] == "nex-agi/nex-n2.5-mini:free"
    assert captured["body"]["messages"][0]["content"] == "查勤工助学岗位"
    assert captured["body"]["tools"][0]["function"]["name"] == "search_service_items"
    assert captured["body"]["parallel_tool_calls"] is False
    assert payload["choices"][0]["message"]["content"] == "ok"


def test_responses_adapter_targets_responses_endpoint_and_native_tool_shape(monkeypatch):
    captured = {}
    transport = httpx.MockTransport(
        lambda request: (
            captured.update({
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": json.loads(request.content),
            })
            or httpx.Response(200, json={"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]})
        )
    )
    real_client = agent.httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(agent.httpx, "Client", client_factory)
    config = AgentConfig(
        provider="compatible",
        api_key="responses-test-secret",
        model="glm-5.2",
        base_url="https://relay.example/v1",
        api_format=None,
        protocol_policy="responses_preferred",
    )
    request = ModelTurnRequest(
        turn_id="turn-responses",
        instructions="回答校园事务",
        input=[
            {"role": "user", "content": "查活动场地"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "provider-call-1",
                    "type": "function",
                    "function": {"name": agent.TOOL_ID, "arguments": '{"query":"查活动场地"}'},
                }],
            },
            {"role": "tool", "tool_call_id": "provider-call-1", "content": '{"selected":{"slug":"venue-application"}}'},
        ],
        tools=[agent.SEARCH_TOOL],
        output_schema={},
        tool_choice={"type": "function", "function": {"name": agent.TOOL_ID}},
    )

    payload = agent._responses_completion(config, request)

    assert captured["url"] == "https://relay.example/v1/responses"
    assert captured["headers"]["authorization"] == "Bearer responses-test-secret"
    assert captured["body"]["model"] == "glm-5.2"
    assert captured["body"]["instructions"] == "回答校园事务"
    assert captured["body"]["tools"][0]["name"] == "search_service_items"
    assert captured["body"]["parallel_tool_calls"] is False
    assert captured["body"]["input"][0]["role"] == "user"
    assert {item["type"] for item in captured["body"]["input"] if "type" in item} >= {"function_call", "function_call_output"}
    assert next(item for item in captured["body"]["input"] if item.get("type") == "function_call_output")["call_id"] == "provider-call-1"
    assert payload["output"][0]["content"][0]["text"] == "ok"


def test_responses_adapter_emits_native_answer_draft_json_schema(monkeypatch):
    captured = {}
    transport = httpx.MockTransport(
        lambda request: (
            captured.update({"body": json.loads(request.content)})
            or httpx.Response(200, json={"status": "completed", "output": []})
        )
    )
    real_client = agent.httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(agent.httpx, "Client", client_factory)
    config = AgentConfig(
        provider="compatible",
        api_key="responses-schema-secret",
        model="glm-5.2",
        base_url="https://relay.example/v1",
        protocol_policy="responses_preferred",
    )

    agent._responses_completion(
        config,
        ModelTurnRequest(
            "turn-answer",
            "回答校园事务",
            [{"role": "user", "content": "查询活动场地"}],
            [],
            agent.ANSWER_DRAFT_JSON_SCHEMA,
        ),
    )

    output_format = captured["body"]["text"]["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["name"] == "answer_draft"
    assert output_format["strict"] is True
    assert output_format["schema"] == agent.ANSWER_DRAFT_JSON_SCHEMA


def test_responses_stream_adapter_rebuilds_safe_native_output(monkeypatch):
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b'event: response.output_item.added\ndata: {"type":"response.output_item.added","sequence_number":1,"item":{"type":"function_call","id":"fc-1","call_id":"call-1","name":"search_service_items","arguments":""}}\n\n'
                b'event: response.function_call_arguments.delta\ndata: {"type":"response.function_call_arguments.delta","sequence_number":2,"item_id":"fc-1","delta":"{\\"query\\":\\"\\u573a\\u5730\\"}"}\n\n'
                b'event: response.function_call_arguments.done\ndata: {"type":"response.function_call_arguments.done","sequence_number":3,"call_id":"call-1"}\n\n'
                b'event: response.completed\ndata: {"type":"response.completed","sequence_number":4,"response":{"id":"resp-1","usage":{"input_tokens":3,"output_tokens":2,"total_tokens":5}}}\n\n'
            ),
        )

    real_client = agent.httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(agent.httpx, "Client", client_factory)
    config = AgentConfig(
        provider="compatible",
        api_key="responses-stream-secret",
        model="glm-5.2",
        base_url="https://relay.example/v1",
        protocol_policy="responses_preferred",
    )

    payload = agent._responses_completion(
        config,
        ModelTurnRequest(
            "turn-responses-stream",
            "回答校园事务",
            [{"role": "user", "content": "查活动场地"}],
            [agent.SEARCH_TOOL],
            {},
            stream=True,
        ),
    )

    assert captured["url"] == "https://relay.example/v1/responses"
    assert captured["body"]["stream"] is True
    assert captured["body"]["parallel_tool_calls"] is False
    assert payload["output"] == [{
        "type": "function_call",
        "call_id": "call-1",
        "name": "search_service_items",
        "arguments": '{"query":"场地"}',
    }]
    assert payload["usage"] == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
    assert "responses-stream-secret" not in json.dumps(payload, ensure_ascii=False)


def test_responses_message_input_variant_uses_compatible_request_shape(monkeypatch):
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
            },
        )

    real_client = agent.httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(agent.httpx, "Client", client_factory)
    config = AgentConfig(
        provider="compatible",
        api_key="responses-variant-secret",
        model="glm-5.2",
        base_url="https://relay.example/v1",
        protocol_policy="responses_preferred",
        responses_request_variant="message_input_max_tokens",
    )

    payload = agent._responses_completion(
        config,
        ModelTurnRequest(
            "turn-responses-variant",
            "系统约束",
            [{"role": "user", "content": "请只回复 OK"}],
            [],
            {},
        ),
    )

    assert payload["output"][0]["type"] == "message"
    assert captured["body"]["max_tokens"] == config.max_output_tokens
    assert "max_output_tokens" not in captured["body"]
    assert "instructions" not in captured["body"]
    assert captured["body"]["input"][0] == {"role": "system", "content": "系统约束"}
    assert "responses-variant-secret" not in json.dumps(captured["body"], ensure_ascii=False)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, json={"error": {"code": "responses_endpoint_not_found", "message": "Responses endpoint not found"}}),
        httpx.Response(400, json={"error": {"code": "unsupported_parameter", "message": "Responses endpoint is not supported"}}),
    ],
)
def test_responses_capability_boundary_is_explicitly_classified(monkeypatch, response):
    real_client = agent.httpx.Client
    transport = httpx.MockTransport(lambda request: response)

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(agent.httpx, "Client", client_factory)
    config = AgentConfig(
        provider="compatible",
        api_key="responses-boundary-secret",
        model="glm-5.2",
        base_url="https://relay.example/v1",
        protocol_policy="responses_preferred",
    )

    with pytest.raises(AgentModelError, match="provider_capability_unsupported"):
        agent._responses_completion(config, ModelTurnRequest("turn-responses", "回答", [{"role": "user", "content": "查询"}], [], {}))


def test_responses_generic_bad_request_does_not_qualify_for_chat_fallback(monkeypatch):
    real_client = agent.httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(400, json={"error": {"code": "invalid_request_error", "message": "invalid schema"}}))

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(agent.httpx, "Client", client_factory)
    config = AgentConfig(
        provider="compatible",
        api_key="responses-boundary-secret",
        model="glm-5.2",
        base_url="https://relay.example/v1",
        protocol_policy="responses_preferred",
    )

    with pytest.raises(AgentModelError, match="provider_http_error"):
        agent._responses_completion(config, ModelTurnRequest("turn-responses", "回答", [{"role": "user", "content": "查询"}], [], {}))


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, json={"error": {"code": "model_not_found", "message": "The model does not exist"}}),
        httpx.Response(400, json={"error": {"code": "unsupported_parameter", "param": "temperature", "message": "temperature is not supported"}}),
        httpx.Response(400, json={"error": {"code": "invalid_json_schema", "message": "The JSON schema is invalid"}}),
        httpx.Response(400, json={"error": {"code": "invalid_request_error", "message": "input is required"}}),
    ],
)
def test_responses_configuration_errors_do_not_qualify_for_chat_fallback(monkeypatch, response):
    real_client = agent.httpx.Client
    transport = httpx.MockTransport(lambda request: response)

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(agent.httpx, "Client", client_factory)
    config = AgentConfig(
        provider="compatible",
        api_key="responses-boundary-secret",
        model="glm-5.2",
        base_url="https://relay.example/v1",
        protocol_policy="responses_preferred",
    )

    with pytest.raises(AgentModelError, match="provider_http_error"):
        agent._responses_completion(config, ModelTurnRequest("turn-responses", "回答", [{"role": "user", "content": "查询"}], [], {}))


def test_provider_redirect_is_blocked_without_following_location(monkeypatch):
    transport = httpx.MockTransport(lambda request: httpx.Response(307, headers={"location": "https://attacker.example"}))
    real_client = agent.httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(agent.httpx, "Client", client_factory)
    config = agent.AgentConfig(
        provider="compatible",
        api_key="redirect-test-secret",
        model="test-model",
        base_url="https://relay.example/v1",
        api_format="chat_completions",
    )

    with pytest.raises(AgentModelError, match="provider_redirect_blocked"):
        agent._chat_completion(config, [{"role": "user", "content": "查询"}])


def test_provider_transient_failure_retries_once_but_unauthorized_does_not(monkeypatch):
    attempts = []
    responses = [
        httpx.Response(503),
        httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}),
    ]
    transport = httpx.MockTransport(lambda request: attempts.append(request) or responses.pop(0))
    real_client = agent.httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(agent.httpx, "Client", client_factory)
    config = AgentConfig(
        provider="openrouter",
        api_key="retry-test-secret",
        model="nex-agi/nex-n2.5-mini:free",
        base_url="https://relay.example/v1",
        api_format="chat_completions",
        retry_count=1,
    )

    payload = agent._chat_completion(config, [{"role": "user", "content": "查询"}])

    assert payload["choices"][0]["message"]["content"] == "ok"
    assert len(attempts) == 2

    unauthorized_transport = httpx.MockTransport(lambda request: httpx.Response(401))

    def unauthorized_client_factory(*args, **kwargs):
        kwargs["transport"] = unauthorized_transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(agent.httpx, "Client", unauthorized_client_factory)
    with pytest.raises(AgentModelError, match="provider_unauthorized"):
        agent._chat_completion(config, [{"role": "user", "content": "查询"}])
