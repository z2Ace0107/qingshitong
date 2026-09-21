from __future__ import annotations

from datetime import date, timedelta
import json
import sqlite3
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.runtime import get_run, run_query


ROLE_TOKENS = {
    "evaluation_engineer": "evaluation-token",
    "business_knowledge_reviewer": "knowledge-token",
    "release_executor": "release-token",
}


def sign_in_role(client: TestClient, role: str) -> str:
    response = client.post("/api/governance/session", headers={"X-QST-Governance-Token": ROLE_TOKENS[role]})
    assert response.status_code == 200
    csrf_token = response.json()["session"]["csrf_token"]
    client.headers.update({"X-CSRF-Token": csrf_token})
    return csrf_token


def future_weekday(days: int = 10) -> str:
    target = date.today() + timedelta(days=days)
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return target.isoformat()


def prepare_release_gate(client: TestClient, publication_id: str) -> dict:
    sign_in_role(client, "evaluation_engineer")
    # The seeded red-team case has no reproducible trace by design, so it is
    # not eligible for the public repair/regression/close lifecycle. Tests
    # that exercise a successful release explicitly establish a clean fixture.
    with db.connect() as connection:
        connection.execute(
            "UPDATE bad_cases SET status = 'closed', close_note = ?, updated_at = datetime('now') "
            "WHERE id = 'bc-seed-venue-confusion'",
            ("测试夹具已隔离种子阻断案例",),
        )
    evaluation = client.post(
        "/api/evaluations/runs",
        json={"publication_id": publication_id, "run_mode": "release"},
    )
    assert evaluation.status_code == 200
    assert all(case["status"] == "pass" for case in evaluation.json()["cases"]), [
        (case["case_id"], case["status"], case.get("failure_categories"))
        for case in evaluation.json()["cases"]
        if case["status"] != "pass"
    ]
    return evaluation.json()


def activate_candidate(client: TestClient, revision_id: str, note: str = "测试发布") -> dict:
    review_csrf = sign_in_role(client, "business_knowledge_reviewer")
    built = client.post(
        f"/api/source-revisions/{revision_id}/review",
        headers={"X-CSRF-Token": review_csrf, "Idempotency-Key": f"source-review-{revision_id}"},
        json={"decision": "approve", "review_note": note, "expected_version": 1},
    )
    assert built.status_code == 200
    candidate = built.json()
    assert candidate["status"] == "awaiting_business_review"

    business_review = client.post(
        f"/api/publications/{candidate['publication_id']}/business-review",
        headers={"X-CSRF-Token": review_csrf, "Idempotency-Key": f"business-review-{candidate['publication_id']}"},
        json={"decision": "approve", "review_note": note, "expected_version": candidate["version"]},
    )
    assert business_review.status_code == 200
    assert business_review.json()["status"] == "ready"

    prepare_release_gate(client, candidate["publication_id"])
    release_csrf = sign_in_role(client, "release_executor")
    activated = client.post(
        f"/api/governance/releases/{candidate['publication_id']}/release",
        headers={"X-CSRF-Token": release_csrf, "Idempotency-Key": f"publication-activate-{candidate['publication_id']}"},
        json={"release_note": note, "expected_version": business_review.json()["version"]},
    )
    assert activated.status_code == 200
    assert activated.json()["status"] == "published"
    return activated.json()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setenv("QST_APP_ENV", "development")
    monkeypatch.setenv(
        "QST_GOVERNANCE_BOOTSTRAP_TOKENS",
        json.dumps(
            {
                "business_approver": "business-token",
                "site_confirmation_recorder": "site-token",
                "archive_operator": "archive-token",
                "evaluation_engineer": ROLE_TOKENS["evaluation_engineer"],
                "business_knowledge_reviewer": ROLE_TOKENS["business_knowledge_reviewer"],
                "release_executor": ROLE_TOKENS["release_executor"],
            }
        ),
    )
    with TestClient(app) as test_client:
        sign_in_role(test_client, "evaluation_engineer")
        yield test_client


def governance_advance(client: TestClient, task_id: str, target_status: str, expected_version: int) -> dict:
    token = "business-token" if target_status != "completed_simulated" else "archive-token"
    session = client.post("/api/governance/session", headers={"X-QST-Governance-Token": token})
    assert session.status_code == 200
    client.headers.update({"X-CSRF-Token": session.json()["session"]["csrf_token"]})
    response = client.post(
        f"/api/governance/tasks/{task_id}/advance",
        headers={"X-CSRF-Token": session.json()["session"]["csrf_token"]},
        json={
            "target_status": target_status,
            "expected_version": expected_version,
            "idempotency_key": f"leave-{target_status}-{expected_version}",
        },
    )
    assert response.status_code == 200
    return response.json()


def test_bootstrap_exposes_published_demo_catalog(client: TestClient):
    response = client.get("/api/bootstrap")

    assert response.status_code == 200
    payload = response.json()
    assert {"venue-application", "course-selection", "work-study", "leave-application"}.issubset(
        {item["slug"] for item in payload["items"]}
    )
    assert payload["publication"]["status"] == "published"
    assert "演示数据" in payload["disclaimer"]


def test_query_returns_evidence_and_boundary_for_venue_request(client: TestClient):
    response = client.post("/api/query", json={"message": "我想在示例活动广场办迎新活动，应该怎么办？", "channel": "standalone_web"})

    assert response.status_code == 200
    payload = response.json()
    answer = payload["response"]
    assert answer["service_item"]["title"] == "活动场地申请"
    assert answer["evidence"]
    assert answer["simulation"] is True
    assert "当前未接入学校真实业务系统" in answer["simulation_disclaimer"]
    assert payload["feedback_ref"].startswith("feedback-ref-")
    assert "trace_id" not in json.dumps(payload, ensure_ascii=False)


def _read_stream_event(lines):
    current = []
    for line in lines:
        if line == "":
            if not current:
                continue
            event_name = next((item[7:] for item in current if item.startswith("event: ")), None)
            data = next((item[6:] for item in current if item.startswith("data: ")), None)
            return event_name, json.loads(data) if data is not None else None
        current.append(line)
    return None, None


def _stream_events(lines):
    events = []
    current = []
    for line in lines:
        if line == "":
            if current:
                event_name = next((item[7:] for item in current if item.startswith("event: ")), None)
                data = next((item[6:] for item in current if item.startswith("data: ")), None)
                if data is not None:
                    events.append((event_name, json.loads(data)))
                current = []
            continue
        current.append(line)
    return events


def test_stream_query_projects_business_progress_and_one_terminal_event(client: TestClient):
    with client.stream(
        "POST",
        "/api/query/stream",
        json={"message": "我想在示例活动广场办迎新活动，应该怎么办？", "channel": "standalone_web"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = _stream_events(response.iter_lines())

    assert events[0][0] == "run.started"
    assert any(name == "progress" for name, _ in events)
    terminal = [payload for _, payload in events if payload["terminal"]]
    assert len(terminal) == 1
    assert terminal[0]["type"] in {"run.completed", "run.degraded", "run.needs_review", "run.refused"}
    assert "publication_id" not in json.dumps(terminal[0], ensure_ascii=False)
    assert "trace_id" not in json.dumps(terminal[0], ensure_ascii=False)
    assert terminal[0]["payload"].get("response", terminal[0]["payload"]).get("simulation_disclaimer")


def test_stream_query_routes_runtime_through_provider_streaming_path(client: TestClient, monkeypatch):
    from app import runtime

    observed = {}
    original = runtime.run_query

    def wrapped(*args, **kwargs):
        observed["streaming"] = kwargs.get("streaming")
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime, "run_query", wrapped)
    events = _stream_events(
        "".join(
            runtime.stream_query(
                "查询活动场地申请",
                "streaming-path-session",
                "standalone_web",
                None,
                agent_config=runtime.AgentConfig(),
            )
        ).splitlines()
    )

    assert observed["streaming"] is True
    assert len([payload for _, payload in events if payload["terminal"]]) == 1


def test_stream_query_maps_conflicting_evidence_to_needs_review(client: TestClient, monkeypatch):
    from app import runtime

    candidate = {
        "slug": "venue-application",
        "title": "活动场地申请",
        "score": 1.0,
        "source_revision_id": "src-venue-v1",
        "freshness_state": "verified_current",
    }
    conflict_meta = {
        "evidence_validation": {
            "valid": False,
            "support_status": "conflicted",
            "conflict_status": "detected",
        }
    }

    monkeypatch.setattr(
        runtime,
        "search",
        lambda *args, **kwargs: {"candidates": [candidate], "meta": conflict_meta},
    )
    monkeypatch.setattr(
        runtime,
        "validate_claim_evidence",
        lambda *args, **kwargs: {
            "valid": False,
            "support_status": "conflicted",
            "conflict_status": "detected",
        },
    )

    with client.stream(
        "POST",
        "/api/query/stream",
        json={"message": "查询活动场地当前办理条件", "channel": "standalone_web"},
    ) as response:
        events = _stream_events(response.iter_lines())

    terminal = [payload for _, payload in events if payload["terminal"]]
    assert len(terminal) == 1
    assert terminal[0]["type"] == "run.needs_review"
    run_id = terminal[0]["run_id"]
    stored = get_run(run_id)
    assert stored is not None
    assert stored["status"] == "needs_review"
    assert "publication_id" not in json.dumps(terminal[0], ensure_ascii=False)


def test_stream_generator_disconnect_marks_run_cancelled_before_worker_starts(client: TestClient):
    from app import runtime

    generator = runtime.stream_query(
        "查询活动场地申请",
        None,
        "standalone_web",
        None,
        agent_config=runtime.AgentConfig(),
    )
    first = next(generator)
    next(generator)
    run_id = json.loads(next(line[6:] for line in first.splitlines() if line.startswith("data: ")))['run_id']

    generator.close()

    run = get_run(run_id)
    assert run is not None
    assert run["status"] == "cancelled"
    assert run["error_code"] == "client_disconnected"


def test_stream_cancel_is_idempotent_and_finishes_as_cancelled(client: TestClient, monkeypatch):
    from app import runtime

    started = Event()
    release = Event()
    run_created = Event()
    run_holder = {}
    original = runtime.run_query
    original_start = runtime.start_stream_run

    def blocked(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    def capture_run(*args, **kwargs):
        run_holder["id"] = args[0]
        result = original_start(*args, **kwargs)
        run_created.set()
        return result

    monkeypatch.setattr(runtime, "run_query", blocked)
    monkeypatch.setattr(runtime, "start_stream_run", capture_run)
    result_holder = {}

    def consume_stream():
        with client.stream(
            "POST",
            "/api/query/stream",
            json={"message": "查询活动场地申请", "channel": "standalone_web"},
        ) as response:
            result_holder["status_code"] = response.status_code
            result_holder["events"] = _stream_events(response.iter_lines())

    stream_thread = Thread(target=consume_stream)
    stream_thread.start()
    assert run_created.wait(3)
    assert started.wait(3)
    run_id = run_holder["id"]
    owned_session_id = get_run(run_id)["session_id"]
    client.cookies.set("qst_student_session", owned_session_id)
    cancel_headers = {"Idempotency-Key": "stream-cancel-001"}
    cancelled = client.post(f"/api/runs/{run_id}/cancel", headers=cancel_headers)
    repeated = client.post(f"/api/runs/{run_id}/cancel", headers=cancel_headers)
    assert cancelled.status_code == 200
    assert repeated.status_code == 200
    assert repeated.json() == cancelled.json()
    with db.connect() as connection:
        persisted = connection.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert persisted["status"] == "cancel_requested"
    release.set()
    stream_thread.join(5)
    events = result_holder["events"]

    assert result_holder["status_code"] == 200
    terminal = [payload for _, payload in events if payload["terminal"]]
    assert len(terminal) == 1
    assert terminal[0]["type"] == "run.cancelled"


def test_student_query_rejects_client_supplied_session_id(client: TestClient):
    response = client.post(
        "/api/query",
        json={"message": "查询活动场地申请", "session_id": "attacker-chosen-session"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_student_cancel_rejects_run_owned_by_another_browser_session(client: TestClient):
    from app import runtime

    run_id = "run-cross-session-cancel"
    runtime.start_stream_run(
        run_id,
        session_id="student-session-a",
        channel="standalone_web",
        message="查询活动场地申请",
    )
    try:
        client.cookies.set("qst_student_session", "student-session-b")
        response = client.post(
            f"/api/runs/{run_id}/cancel",
            headers={"Idempotency-Key": "cross-session-cancel-001"},
        )

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "run_session_forbidden"
        stored = get_run(run_id)
        assert stored is not None
        assert stored["status"] == "running"
    finally:
        with db.connect() as connection:
            connection.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        with runtime._STREAM_LOCK:
            runtime._STREAM_CONTROLS.pop(run_id, None)


def test_stream_query_fails_closed_when_final_response_exceeds_buffer_limit(client: TestClient, monkeypatch):
    from app import runtime

    def oversized_result(*args, **kwargs):
        return {
            "trace_id": "trace-oversized-response",
            "response": {
                "kind": "answer",
                "summary": "x" * (128 * 1024 + 1),
                "next_actions": ["重新提交"],
                "simulation": False,
            },
        }

    monkeypatch.setattr(runtime, "run_query", oversized_result)
    stream = runtime.stream_query(
        "查询活动场地",
        None,
        "standalone_web",
        None,
        agent_config=runtime.AgentConfig(),
    )
    events = _stream_events("".join(stream).splitlines())
    terminal = [payload for _, payload in events if payload["terminal"]]

    assert len(terminal) == 1
    assert terminal[0]["type"] == "run.failed"
    run = get_run(terminal[0]["run_id"])
    assert run["status"] == "failed"
    assert run["error_code"] == "final_response_too_large"
    assert run["context_manifest"]["stream_config"]["version"] == "qst.stream-config.v1"
    assert run["context_manifest"]["stream_config"]["max_final_response_bytes"] == 128 * 1024


def test_stream_config_is_versioned_and_matches_qp3_contract():
    from app.runtime import stream_config

    assert stream_config() == {
        "version": "qst.stream-config.v1",
        "heartbeat_seconds": 15.0,
        "max_event_bytes": 64 * 1024,
        "max_final_response_bytes": 128 * 1024,
    }


def test_student_query_rejects_page_provider_credentials(client: TestClient):
    secret = "trace-persistence-test-key"
    response = client.post(
        "/api/query",
        json={
            "message": "我想申请病假，需要准备什么？",
            "agent_provider": "deepseek",
            "api_key": secret,
            "base_url": "https://relay.example/v1",
            "model": "deepseek-flash",
            "api_format": "responses",
        },
    )

    assert response.status_code == 422
    assert secret not in json.dumps(response.json(), ensure_ascii=False)


def test_query_exposes_versioned_service_response_contract(client: TestClient):
    response = client.post(
        "/api/query",
        json={"message": "我想在示例活动广场 2026年10月1日 10:00-12:00 办迎新活动，应该怎么办？", "channel": "standalone_web"},
    )

    assert response.status_code == 200
    payload = response.json()
    answer = payload["response"]
    assert answer["status"] == "answer"
    assert answer["simulation"] is True
    assert "publication_id" not in json.dumps(payload, ensure_ascii=False)
    assert "trace_id" not in json.dumps(payload, ensure_ascii=False)
    assert answer["simulation_boundary"] == {
        "simulation": True,
        "real_integration": False,
        "label": "当前未接入学校真实业务系统",
    }

    for evidence in answer["evidence"]:
        assert evidence["source_title"]
        assert "source_revision_id" not in evidence
        assert "chunk_id" not in evidence
        assert evidence["freshness_state"] == "verified_current"


def test_vague_venue_request_asks_for_minimal_clarification(client: TestClient):
    response = client.post(
        "/api/query",
        json={"message": "我想申请多功能活动室场地", "channel": "standalone_web"},
    )

    assert response.status_code == 200
    answer = response.json()["response"]

    assert answer["status"] == "clarify"
    assert answer["kind"] == "clarify"
    assert answer["service_item"]["title"] == "活动场地申请"
    assert answer["clarifications"] == [
        {"field": "date", "question": "计划哪一天使用场地？"},
        {"field": "start_time", "question": "计划使用的开始时间和结束时间是什么？"},
    ]
    assert "校园卡" not in answer["summary"]


def test_query_refuses_unknown_request_instead_of_inventing_policy(client: TestClient):
    response = client.post("/api/query", json={"message": "我想办一个系统里没有的校园事务"})

    assert response.status_code == 200
    payload = response.json()
    answer = payload["response"]
    assert answer["kind"] == "refused"
    assert answer["status"] == "refuse"
    assert answer["evidence"] == []
    assert "publication_id" not in answer
    assert answer["simulation_boundary"]["real_integration"] is False
    assert "编造" in answer["freshness"]["message"]


def test_query_keeps_personal_course_result_at_official_boundary(client: TestClient):
    response = client.post("/api/query", json={"message": "帮我查一下我个人的选课结果"})

    assert response.status_code == 200
    answer = response.json()["response"]
    assert answer["service_item"]["title"] == "选课公告与课表入口"
    assert answer["simulation"] is False
    assert "个人选课结果" in answer["simulation_disclaimer"]
    assert "模拟" not in answer["simulation_disclaimer"]
    assert answer["official_entry"]["url"].startswith("https://demo.")


def test_leave_query_remains_information_only_outside_r0_venue_scope(client: TestClient):
    response = client.post(
        "/api/query",
        json={"message": "我想申请病假，应该准备什么？", "channel": "standalone_web"},
    )

    assert response.status_code == 200
    answer = response.json()["response"]
    assert answer["service_item"]["title"] == "本科生请假与销假"
    assert answer["simulation"] is False
    assert answer["next_action"] == "open_official_entry"
    assert "scenario_id" not in answer
    assert answer["simulation_boundary"]["real_integration"] is False


def test_runtime_exposes_allowlisted_tools_and_manifest_binds_to_it(client: TestClient):
    tools_response = client.get("/api/runtime/tools")
    assert tools_response.status_code == 200
    tools = tools_response.json()["tools"]
    tool_ids = {tool["tool_id"] for tool in tools}

    assert {
        "search_service_items",
        "check_service_rules",
        "precheck_simulation_materials",
        "create_simulation_draft",
        "confirm_simulation_submission",
        "record_feedback",
    }.issubset(tool_ids)
    assert "real_school_write" not in tool_ids
    assert all(tool["real_integration"] is False for tool in tools)
    assert all("evaluation_engineer" in tool["allowed_scopes"] for tool in tools)
    assert all("developer_evaluator" not in tool["allowed_scopes"] for tool in tools)

    query = client.post("/api/query", json={"message": "我想查勤工助学岗位"}).json()
    assert set(query) == {"response", "feedback_ref"}
    assert "context_manifest" not in query


def test_run_trace_can_be_replayed_with_persisted_steps(client: TestClient):
    replay = client.get("/api/runs/trace-not-for-students")
    assert replay.status_code == 403
    assert replay.json()["detail"]["code"] == "governance_auth_required"

    internal = run_query("我想查勤工助学岗位", "test-session", "standalone_web")
    payload = get_run(internal["trace_id"])
    assert payload["status"] == "completed"
    assert payload["id"] == internal["trace_id"]
    assert payload["publication_id"]
    assert payload["response"]["trace_id"] == internal["trace_id"]
    assert payload["steps"]
    assert all(step["run_id"] == internal["trace_id"] for step in payload["steps"])
    assert all(step["step_id"] for step in payload["steps"])


def test_governance_can_read_run_trace_but_student_endpoint_remains_blocked(client: TestClient):
    internal = run_query("我想查勤工助学岗位", "governance-trace-session", "standalone_web")

    student_view = client.get(f"/api/runs/{internal['trace_id']}")
    assert student_view.status_code == 403
    assert student_view.json()["detail"]["code"] == "governance_auth_required"

    governance_view = client.get(f"/api/governance/runs/{internal['trace_id']}")
    assert governance_view.status_code == 200
    payload = governance_view.json()
    assert payload["id"] == internal["trace_id"]
    assert payload["steps"]
    assert payload["context_manifest"]["knowledge_publication_id"]


def test_runtime_persists_protocol_trace_but_student_projection_omits_it(client: TestClient, monkeypatch):
    from app import runtime

    monkeypatch.setattr(
        runtime,
        "run_agent_loop",
        lambda *args, **kwargs: {
            "mode": "api",
            "status": "completed",
            "fallback": False,
            "turns": 2,
            "api_format": "chat_completions",
            "protocol_attempts": ["responses", "chat_completions"],
            "protocol_fallback": True,
            "fallback_reason": "provider_capability_unsupported",
            "retrieval_query": "勤工助学",
            "retrieval_result": None,
            "answer_draft": None,
            "tool_calls": [{"tool_call_id": "runtime-tool-1", "status": "succeeded"}],
        },
    )

    internal = runtime.run_query("我想查勤工助学岗位", "protocol-trace-session", "standalone_web")
    stored = get_run(internal["trace_id"])

    assert stored["response"]["agent"]["api_format"] == "chat_completions"
    assert stored["response"]["agent"]["protocol_attempts"] == ["responses", "chat_completions"]
    assert stored["response"]["agent"]["protocol_fallback"] is True
    assert stored["response"]["agent"]["fallback_reason"] == "provider_capability_unsupported"
    assert "agent" not in runtime.student_response_view(internal["response"])

    sign_in_role(client, "evaluation_engineer")
    governance = client.get(f"/api/governance/runs/{internal['trace_id']}")
    assert governance.status_code == 200
    assert governance.json()["response"]["agent"]["protocol_attempts"] == ["responses", "chat_completions"]


def test_runtime_trace_redacts_credentials_from_persisted_input(client: TestClient):
    secret = "qst-test-runtime-secret-123456789"
    internal = run_query(f"请处理 {secret} 对应的校园事项", "trace-redaction-session", "standalone_web")

    stored = get_run(internal["trace_id"])
    assert stored is not None
    serialized = json.dumps(stored, ensure_ascii=False)
    assert secret not in serialized
    assert "[REDACTED]" in stored["input_text"]


def test_runtime_run_status_matches_clarification_and_refusal_semantics(client: TestClient):
    clarification = run_query("我想申请多功能活动室场地", "test-session", "standalone_web")
    refusal = run_query("我想办一个系统里没有的校园事务", "test-session", "standalone_web")

    clarification_run = get_run(clarification["trace_id"])
    refusal_run = get_run(refusal["trace_id"])

    assert clarification["response"]["next_action"] == "ask_user"
    assert clarification_run["status"] == "waiting_user"
    assert refusal["response"]["next_action"] == "open_official_entry"
    assert refusal_run["status"] == "refused"


def test_runtime_replay_uses_the_requested_publication_snapshot(client: TestClient):
    from app.runtime import run_query

    previous = client.get("/api/bootstrap").json()["publication"]["id"]
    changed = client.post("/api/demo/announcement-change", json={"source_id": "src-venue-v1"}).json()
    published = activate_candidate(client, changed["candidate_revision_id"], "固定版本回放演示")

    replay = run_query(
        "我想借教室办迎新会",
        session_id="replay-session",
        channel="evaluation",
        publication_id=previous,
    )

    assert published["publication_id"] != previous
    assert replay["context_manifest"]["knowledge_publication_id"] == previous
    assert replay["response"]["publication_id"] == previous
    assert replay["response"]["evidence"][0]["revision_id"] == "src-venue-v1"


def test_current_publication_query_uses_new_revision_after_publish(client: TestClient):
    previous = client.get("/api/bootstrap").json()["publication"]["id"]
    changed = client.post("/api/demo/announcement-change", json={"source_id": "src-venue-v1"}).json()
    published = activate_candidate(client, changed["candidate_revision_id"], "当前版本读取演示")

    response = run_query("我想借教室办迎新会", "current-session", "standalone_web")

    assert published["publication_id"] != previous
    assert response["response"]["publication_id"] == published["publication_id"]
    assert response["response"]["evidence"][0]["revision_id"] == changed["candidate_revision_id"]


def test_publication_index_failure_keeps_previous_release(client: TestClient, monkeypatch):
    previous = client.get("/api/bootstrap").json()["publication"]["id"]
    changed = client.post("/api/demo/announcement-change", json={"source_id": "src-venue-v1"}).json()

    from app import publication

    def fail_index(*args, **kwargs):
        raise RuntimeError("fts build failed")

    monkeypatch.setattr(publication, "build_keyword_index", fail_index)
    review_csrf = sign_in_role(client, "business_knowledge_reviewer")
    response = client.post(
        f"/api/source-revisions/{changed['candidate_revision_id']}/review",
        headers={"X-CSRF-Token": review_csrf, "Idempotency-Key": f"source-review-{changed['candidate_revision_id']}"},
        json={"decision": "approve", "review_note": "索引失败回滚", "expected_version": 1},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "publication_build_failed"
    assert client.get("/api/bootstrap").json()["publication"]["id"] == previous
    with db.connect() as connection:
        source = connection.execute(
            "SELECT status FROM source_revisions WHERE id = ?",
            (changed["candidate_revision_id"],),
        ).fetchone()
        failed = connection.execute(
            "SELECT status FROM publications WHERE id = ?",
            (response.json()["detail"]["publication_id"],),
        ).fetchone()
    assert source["status"] == "approved"
    assert failed["status"] == "checks_failed"


def test_rag_rewrites_campus_synonyms_and_reports_strategy_stages(client: TestClient):
    result = run_query("我想借教室办迎新会", "rag-session", "standalone_web")
    retrieval = result["retrieval"]
    assert result["response"]["service_item"]["slug"] == "venue-application"
    assert retrieval["strategy"] == "c"
    assert retrieval["query_rewrite"]["original"] == "我想借教室办迎新会"
    assert {"场地", "办活动"}.issubset(set(retrieval["query_rewrite"]["added_terms"]))
    assert retrieval["stages"] == [
        "query_rewrite",
        "hard_filter",
        "sparse",
        "dense_unavailable",
        "degraded_sparse",
        "relation_unavailable",
        "evidence_validation",
    ]
    assert retrieval["retrieval_state"] == "degraded_sparse"
    assert retrieval["dense_available"] is False
    assert retrieval["evidence_validation"]["valid"] is True


def test_runtime_refuses_candidates_when_evidence_gate_is_not_valid(client: TestClient, monkeypatch):
    from app import runtime

    publication_id = client.get("/api/bootstrap").json()["publication"]["id"]
    candidate = {
        "slug": "venue-application",
        "title": "场地申请",
        "score": 1.0,
        "source_revision_id": "src-venue-v1",
        "freshness_state": "verified_current",
        "evidence_id": "evidence-blocked",
        "citation_allowed": False,
        "support_claims": ["venue-application"],
    }
    retrieval_result = {
        "candidates": [candidate],
        "meta": {
            "strategy": "a",
            "query_rewrite": {"original": "示例活动广场", "rewritten": "示例活动广场", "added_terms": []},
            "stages": ["query_rewrite", "hard_filter", "sparse", "evidence_validation"],
            "candidate_count": 1,
            "sparse_count": 1,
            "dense_count": 0,
            "dense_available": False,
            "retrieval_state": "keyword",
            "index_status": "ready",
            "dense_index_status": "missing",
            "evidence_validation": {
                "valid": False,
                "support_status": "insufficient",
                "conflict_status": "none",
                "unsupported_claims": ["venue-application"],
            },
        },
    }

    monkeypatch.setattr(
        runtime,
        "run_agent_loop",
        lambda *args, **kwargs: {
            "mode": "deterministic",
            "status": "completed",
            "fallback": False,
            "turns": 0,
            "retrieval_query": "示例活动广场",
            "retrieval_result": retrieval_result,
            "tool_calls": [],
        },
    )

    result = runtime.run_query("示例活动广场的申请规则是什么？", "evidence-gate-session", "standalone_web")

    assert result["response"]["kind"] == "refused"
    assert result["response"]["evidence"] == []
    assert result["response"]["next_action"] == "open_official_entry"
    assert any(step["name"] == "证据门" and step["status"] == "blocked" for step in result["steps"])


def test_runtime_routes_conflicting_evidence_to_verification(client: TestClient, monkeypatch):
    from app import runtime

    candidate = {
        "slug": "venue-application",
        "title": "场地申请",
        "score": 1.0,
        "source_revision_id": "src-venue-v1",
        "freshness_state": "verified_current",
        "evidence_id": "evidence-conflicted",
        "citation_allowed": True,
        "support_claims": ["venue-application"],
    }
    retrieval_result = {
        "candidates": [candidate],
        "meta": {
            "strategy": "a",
            "query_rewrite": {"original": "示例活动广场", "rewritten": "示例活动广场", "added_terms": []},
            "stages": ["query_rewrite", "hard_filter", "sparse", "evidence_validation"],
            "candidate_count": 1,
            "sparse_count": 1,
            "dense_count": 0,
            "dense_available": False,
            "retrieval_state": "keyword",
            "index_status": "ready",
            "dense_index_status": "missing",
            "evidence_validation": {
                "valid": False,
                "support_status": "conflicted",
                "conflict_status": "detected",
                "unsupported_claims": [],
                "conflicting_claims": ["venue:time_window"],
            },
        },
    }

    monkeypatch.setattr(
        runtime,
        "run_agent_loop",
        lambda *args, **kwargs: {
            "mode": "deterministic",
            "status": "completed",
            "fallback": False,
            "turns": 0,
            "retrieval_query": "示例活动广场",
            "retrieval_result": retrieval_result,
            "tool_calls": [],
        },
    )

    result = runtime.run_query("示例活动广场的开放时段是什么？", "evidence-conflict-session", "standalone_web")

    assert result["response"]["kind"] == "refused"
    assert result["response"]["title"] == "当前依据需要核验"
    assert result["response"]["next_action"] == "open_official_entry"
    assert result["response"]["evidence"] == []
    assert any(step["name"] == "证据门" and step["status"] == "blocked" for step in result["steps"])


def test_venue_task_requires_precheck_preview_confirm_and_is_idempotent(client: TestClient):
    future_date = future_weekday()
    task_response = client.post("/api/tasks", json={"scenario_id": "scenario-venue-v1", "channel": "portal_sim", "form_data": {}})
    assert task_response.status_code == 200
    task = task_response.json()
    task_id = task["id"]
    assert task["status"] == "draft"

    form = {
        "event_name": "迎新交流会",
        "date": future_date,
        "start_time": "10:00",
        "end_time": "12:00",
        "attendees": 80,
        "organizer": "学生组织（演示）",
        "materials_confirmed": True,
    }
    checked = client.post(f"/api/tasks/{task_id}/precheck", json={"form_data": form}).json()
    assert checked["status"] == "ready_for_preview"
    preview = client.post(f"/api/tasks/{task_id}/preview").json()
    assert preview["preview"]["proposed_status"] == "submitted_simulated"
    assert preview["task"]["status"] == "ready_for_preview"

    confirmed = client.post(f"/api/tasks/{task_id}/confirm", json={"idempotency_key": "demo-confirm-001", "confirmed": True}).json()
    assert confirmed["status"] == "submitted_simulated"
    repeated = client.post(f"/api/tasks/{task_id}/confirm", json={"idempotency_key": "demo-confirm-001", "confirmed": True}).json()
    assert repeated["id"] == task_id
    assert repeated["status"] == "submitted_simulated"
    assert repeated["submission_snapshot"]["simulation"] is True


def test_r0_student_task_entry_only_allows_venue_venue_workflow(client: TestClient):
    response = client.post(
        "/api/student/tasks",
        json={"scenario_id": "scenario-workstudy-v1", "channel": "standalone_web"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "r0_scenario_not_available"


def test_simulation_materials_block_preview_until_explicitly_confirmed(client: TestClient):
    future_date = future_weekday()
    task_id = client.post("/api/tasks", json={"scenario_id": "scenario-venue-v1"}).json()["id"]
    form = {
        "event_name": "社团活动",
        "date": future_date,
        "start_time": "10:00",
        "end_time": "12:00",
        "attendees": 30,
        "organizer": "学生组织（演示）",
    }

    blocked = client.post(f"/api/tasks/{task_id}/precheck", json={"form_data": form}).json()
    assert blocked["status"] == "precheck_failed"
    assert all(item["status"] == "待确认" for item in blocked["material_results"])

    preview = client.post(f"/api/tasks/{task_id}/preview")
    assert preview.status_code == 409
    assert preview.json()["detail"]["code"] == "task_not_ready_for_preview"

    ready = client.post(
        f"/api/tasks/{task_id}/precheck",
        json={"form_data": {**form, "materials_confirmed": True}},
    ).json()
    assert ready["status"] == "ready_for_preview"
    assert all(item["status"] == "已确认（演示）" for item in ready["material_results"])


def test_venue_precheck_accepts_injected_reference_date(client: TestClient):
    from app.simulation import precheck_task

    task_id = client.post("/api/tasks", json={"scenario_id": "scenario-venue-v1"}).json()["id"]
    checked = precheck_task(
        task_id,
        {
            "event_name": "稳定日期演示",
            "date": "2026-09-17",
            "start_time": "10:00",
            "end_time": "12:00",
            "attendees": 20,
            "organizer": "学生组织（演示）",
            "materials_confirmed": True,
        },
        today=date(2026, 9, 14),
    )

    assert checked["status"] == "ready_for_preview"


def test_task_rejects_confirmation_before_ready_state(client: TestClient):
    task_id = client.post("/api/tasks", json={"scenario_id": "scenario-venue-v1"}).json()["id"]

    response = client.post(f"/api/tasks/{task_id}/confirm", json={"idempotency_key": "too-early", "confirmed": True})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "confirmation_not_allowed"


def test_workstudy_precheck_reports_missing_required_data(client: TestClient):
    task_id = client.post("/api/tasks", json={"scenario_id": "scenario-workstudy-v1"}).json()["id"]

    response = client.post(f"/api/tasks/{task_id}/precheck", json={"form_data": {"job_id": "job-library"}})

    assert response.status_code == 200
    task = response.json()
    assert task["status"] == "precheck_failed"
    assert "availability" in task["missing_fields"]
    assert any(result["rule_id"] == "workstudy-qualification" and not result["passed"] for result in task["rule_results"])


def test_leave_precheck_selects_material_by_leave_type(client: TestClient):
    task_id = client.post("/api/tasks", json={"scenario_id": "scenario-leave-v1"}).json()["id"]

    missing_proof = client.post(
        f"/api/tasks/{task_id}/precheck",
        json={
            "form_data": {
                "leave_type": "medical",
                "start_date": "2026-09-20",
                "end_date": "2026-09-21",
                "reason": "身体不适",
                "evidence_confirmed": False,
            }
        },
    ).json()

    assert missing_proof["status"] == "precheck_failed"
    material = next(item for item in missing_proof["material_results"] if item["rule_id"] == "leave-proof")
    assert material["name"] == "医疗或授权证明材料"
    assert material["status"] == "待确认"
    assert any(
        result["rule_id"] == "leave-proof" and not result["passed"]
        for result in missing_proof["rule_results"]
    )

    ready = client.post(
        f"/api/tasks/{task_id}/precheck",
        json={"form_data": {"evidence_confirmed": True}},
    ).json()

    assert ready["status"] == "ready_for_preview"
    material = next(item for item in ready["material_results"] if item["rule_id"] == "leave-proof")
    assert material["status"] == "已确认（演示）"


def test_announcement_candidate_waits_for_publication_before_marking_tasks(client: TestClient):
    task_id = client.post("/api/tasks", json={"scenario_id": "scenario-venue-v1"}).json()["id"]

    response = client.post("/api/demo/announcement-change")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "pending_review"
    assert payload["affected_tasks"] == []
    changed = client.get(f"/api/tasks/{task_id}").json()
    assert changed["status"] == "draft"
    assert changed["impact_flags"] == []


def test_negative_feedback_creates_bad_case(client: TestClient):
    query = client.post("/api/query", json={"message": "我想查勤工助学岗位"}).json()
    response = client.post(
        "/api/feedback",
        json={"response_id": query["feedback_ref"], "rating": "not_helpful", "reason": "入口不清楚"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["bad_case_id"]
    cases = client.get("/api/bad-cases").json()["bad_cases"]
    assert any(case["id"] == payload["bad_case_id"] and case["status"] == "open" for case in cases)


def test_negative_feedback_redacts_credentials_from_bad_case_snapshot(client: TestClient):
    secret = "qst-test-feedback-secret-123456789"
    query = client.post("/api/query", json={"message": f"请复核 {secret} 对应的回答"}).json()
    response = client.post(
        "/api/feedback",
        json={
            "response_id": query["feedback_ref"],
            "rating": "not_helpful",
            "reason": f"Bearer {secret}",
            "comment": f"api_key={secret}",
        },
    )

    assert response.status_code == 200
    case = next(
        item for item in client.get("/api/bad-cases").json()["bad_cases"]
        if item["id"] == response.json()["bad_case_id"]
    )
    serialized = json.dumps(case, ensure_ascii=False)
    assert secret not in serialized
    assert "[REDACTED]" in serialized


def test_evaluation_run_returns_layered_result_and_gate(client: TestClient):
    response = client.post("/api/evaluations/run", json={"dataset_version": "core12-v1"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["total"] == 12
    assert payload["summary"]["counts"]["error"] == 0
    assert payload["summary"]["quality_gate"] == "pass"
    assert all(set(case["layers"]) == {"retrieval", "process", "response", "business"} for case in payload["cases"])


def test_evaluation_result_exposes_canonical_l1_l4_and_legacy_projection(client: TestClient):
    response = client.post("/api/evaluations/run", json={"dataset_version": "core12-v1", "run_mode": "smoke"})

    assert response.status_code == 200
    case = response.json()["cases"][0]
    assert set(case["canonical_layers"]) == {"L1", "L2", "L3", "L4"}
    assert set(case["layer_results"]).issuperset({"L1", "L2", "L3", "L4"})
    assert set(case["layers"]) == {"retrieval", "process", "response", "business"}
    assert set(response.json()["summary"]["layer_pass_rates"]) == {"L1", "L2", "L3", "L4"}


def test_governance_bad_case_create_sanitizes_and_replay_locks_one_case(client: TestClient):
    run = client.post("/api/evaluations/run", json={"dataset_version": "core12-v1", "run_mode": "smoke"}).json()
    trace_id = run["results"][0]["trace_id"]
    csrf = sign_in_role(client, "evaluation_engineer")
    headers = {"X-CSRF-Token": csrf, "Idempotency-Key": "manual-bad-case-001"}
    body = {
        "origin": "manual_review",
        "input_text": "请复核 qst-test-input-secret-123456789 的回答",
        "trace_id": trace_id,
        "publication_id": run["publication_id"],
        "category": "response",
        "severity": "P1",
        "expected": {"case_id": "core-01", "case_version": "v1", "kind": "answer", "note": "Bearer qst-test-expected-secret-123456789"},
        "actual": {"kind": "answer", "detail": "api_key=qst-test-actual-secret-123456789"},
    }

    created = client.post("/api/governance/bad-cases", headers=headers, json=body)
    assert created.status_code == 200
    value = created.json()
    assert value["status"] == "reproducible"
    assert "qst-test-input-secret" not in value["input_text"]
    assert "[REDACTED]" in value["sanitized_input"]
    serialized = json.dumps(value, ensure_ascii=False)
    assert "qst-test-expected-secret" not in serialized
    assert "qst-test-actual-secret" not in serialized
    assert "[REDACTED]" in serialized

    repeated = client.post("/api/governance/bad-cases", headers=headers, json=body)
    assert repeated.status_code == 200
    assert repeated.json()["id"] == value["id"]

    replay = client.post(
        f"/api/governance/bad-cases/{value['id']}/replay",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "manual-bad-case-replay-001"},
        json={"expected_version": value["version"]},
    )
    assert replay.status_code == 200
    replay_run = replay.json()["replay"]
    assert replay_run["run_mode"] == "replay"
    assert len(replay_run["manifest"]["case_refs"]) == 1
    assert replay_run["manifest"]["case_refs"][0]["case_id"] == "core-01"
    assert replay_run["id"] != run["id"]
    assert replay_run["parent_run_id"] == run["id"]


def test_governance_bad_case_replay_rejects_trace_without_matching_evaluation_result(client: TestClient):
    csrf = sign_in_role(client, "evaluation_engineer")
    created = client.post(
        "/api/governance/bad-cases",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "manual-bad-case-invalid-trace"},
        json={
            "origin": "manual_review",
            "input_text": "没有对应评测结果的案例",
            "trace_id": "runtime-only-trace",
            "publication_id": client.get("/api/bootstrap").json()["publication"]["id"],
            "category": "response",
            "severity": "P1",
            "expected": {"case_id": "core-01", "case_version": "v1", "kind": "answer"},
            "actual": {"kind": "answer"},
        },
    )
    assert created.status_code == 200
    assert created.json()["status"] == "new"

    replay = client.post(
        f"/api/governance/bad-cases/{created.json()['id']}/replay",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "manual-bad-case-invalid-trace-replay"},
        json={"expected_version": created.json()["version"]},
    )

    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "bad_case_not_reproducible"


def test_evaluation_runner_persists_manifest_results_and_attempts(client: TestClient):
    response = client.post(
        "/api/evaluations/runs",
        json={"dataset_version": "core12-v1", "run_mode": "release"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_mode"] == "release"
    assert payload["manifest_hash"].startswith("sha256:")
    assert payload["manifest"]["dataset_version"] == "core12-v1"
    assert payload["manifest"]["publication_id"] == payload["publication_id"]
    assert len(payload["manifest"]["case_refs"]) == payload["total_cases"] == 12
    assert payload["completed_cases"] == 12
    assert payload["attempt_count"] == 12
    assert all(ref["case_version"] == "v1" for ref in payload["manifest"]["case_refs"])
    assert all(case["status"] in {"pass", "fail", "error", "skip", "unscored"} for case in payload["cases"])
    assert all(case["attempt_ids"] for case in payload["cases"])

    detail = client.get(f"/api/evaluations/runs/{payload['id']}")
    assert detail.status_code == 200
    detail_payload = detail.json()
    assert detail_payload["manifest"] == payload["manifest"]
    assert len(detail_payload["results"]) == 12
    assert all(result["run_id"] == payload["id"] for result in detail_payload["results"])
    assert all(result["attempts"] for result in detail_payload["results"])


def test_smoke_run_is_health_only_and_not_quality_gate_eligible(client: TestClient):
    response = client.post(
        "/api/evaluations/runs",
        json={"dataset_version": "core12-v1", "run_mode": "smoke"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_mode"] == "smoke"
    assert payload["quality_gate_eligible"] is False
    assert payload["summary"]["quality_gate"] == "not_applicable"


def test_evaluation_keeps_skip_and_unscored_out_of_the_effective_denominator(client: TestClient, monkeypatch):
    from app import evaluation

    monkeypatch.setattr(
        evaluation,
        "CASES",
        [
            *evaluation.CASES,
            {
                "id": "case-skip-01",
                "split": "challenge",
                "input": "这条案例按数据集规则暂不执行",
                "expected_slug": None,
                "expected_kind": "refused",
                "execution": "skip",
                "skip_reason": "等待业务知识审核人补充前置条件",
            },
            {
                "id": "case-unscored-01",
                "split": "challenge",
                "input": "这条案例有运行结果但暂时没有足够真值",
                "expected_slug": "course-selection",
                "expected_kind": "answer",
                "scorable": False,
            },
        ],
    )

    response = client.post("/api/evaluations/runs", json={"dataset_version": "core12-v1"})

    assert response.status_code == 200
    payload = response.json()
    skipped = next(item for item in payload["cases"] if item["case_id"] == "case-skip-01")
    unscored = next(item for item in payload["cases"] if item["case_id"] == "case-unscored-01")
    assert skipped["status"] == "skip"
    assert skipped["attempt_ids"] == []
    assert "前置条件" in skipped["program_assertions"][0]["reason"]
    assert unscored["status"] == "unscored"
    assert unscored["attempt_ids"]
    assert payload["summary"]["counts"]["skip"] == 1
    assert payload["summary"]["counts"]["unscored"] == 1
    assert payload["summary"]["effective_denominator"] == payload["summary"]["counts"]["pass"] + payload["summary"]["counts"]["fail"]
    assert payload["summary"]["quality_gate"] == "needs_review"


def test_optional_judge_is_persisted_without_overriding_validator_result(client: TestClient, monkeypatch):
    from app import evaluation

    original = evaluation.run_query

    def incomplete_language(*args, **kwargs):
        result = original(*args, **kwargs)
        response = dict(result["response"])
        response["next_actions"] = []
        result["response"] = response
        return result

    monkeypatch.setattr(evaluation, "run_query", incomplete_language)
    response = client.post(
        "/api/evaluations/runs",
        json={"dataset_version": "core12-v1", "run_mode": "smoke", "judge_profile": "deterministic-semantic-v1"},
    )

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["status"] == "pass"
    detail = client.get(f"/api/governance/evaluations/results/{result['id']}").json()
    assert detail["judge_results"]
    judge = detail["judge_results"][0]
    assert judge["judge_profile"] == "deterministic-semantic-v1"
    assert judge["status"] == "fail"
    assert detail["status"] == "pass"


def test_evaluation_failure_creates_a_traceable_bad_case(client: TestClient, monkeypatch):
    from app import evaluation

    original = evaluation.run_query

    def wrong_route(*args, **kwargs):
        result = original(*args, **kwargs)
        response = dict(result["response"])
        response["service_item"] = {"slug": "work-study", "title": "勤工助学"}
        result["response"] = response
        return result

    monkeypatch.setattr(evaluation, "run_query", wrong_route)
    run = client.post("/api/evaluations/runs", json={"dataset_version": "core12-v1", "run_mode": "smoke"}).json()
    failed = next(item for item in run["results"] if item["case_id"] == "core-01")

    assert failed["status"] == "fail"
    assert failed["bad_case_id"]
    cases = client.get("/api/bad-cases").json()["bad_cases"]
    bad_case = next(item for item in cases if item["id"] == failed["bad_case_id"])
    assert bad_case["origin"] == "evaluation"
    assert bad_case["trace_id"] == failed["trace_id"]
    assert bad_case["publication_id"] == run["publication_id"]
    assert bad_case["status"] == "open"
    assert bad_case["expected"]["case_id"] == "core-01"


def test_transient_evaluation_failure_creates_a_new_attempt_without_overwriting_result(client: TestClient, monkeypatch):
    from app import evaluation

    original = evaluation.run_query
    attempts = {"count": 0}

    def flaky(*args, **kwargs):
        if attempts["count"] == 0:
            attempts["count"] += 1
            raise TimeoutError("temporary provider timeout")
        return original(*args, **kwargs)

    monkeypatch.setattr(evaluation, "run_query", flaky)
    response = client.post("/api/evaluations/runs", json={"dataset_version": "core12-v1"})

    assert response.status_code == 200
    payload = response.json()
    first = next(case for case in payload["cases"] if case["case_id"] == "core-01")
    assert len(first["attempt_ids"]) == 2
    assert payload["attempt_count"] == 13
    detail = client.get(f"/api/evaluations/runs/{payload['id']}").json()
    stored_first = next(result for result in detail["results"] if result["case_id"] == "core-01")
    assert [attempt["attempt_no"] for attempt in stored_first["attempts"]] == [1, 2]
    assert stored_first["status"] == "pass"


def test_governance_runner_pause_and_resume_keep_the_original_manifest(client: TestClient, monkeypatch):
    from app import evaluation

    started = Event()
    release = Event()
    original = evaluation.run_query

    def blocked(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(evaluation, "run_query", blocked)
    response = client.post(
        "/api/governance/evaluations/runs",
        json={"dataset_version": "core12-v1", "run_mode": "smoke"},
    )
    assert response.status_code == 202
    queued = response.json()
    assert started.wait(3)

    before_pause = client.get(f"/api/governance/evaluations/runs/{queued['id']}").json()
    pause_request = {"expected_version": before_pause["version"]}
    paused = client.post(
        f"/api/governance/evaluations/runs/{queued['id']}/pause",
        headers={"Idempotency-Key": "evaluation-pause-001"},
        json=pause_request,
    )
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"

    repeated_pause = client.post(
        f"/api/governance/evaluations/runs/{queued['id']}/pause",
        headers={"Idempotency-Key": "evaluation-pause-001"},
        json=pause_request,
    )
    assert repeated_pause.status_code == 200
    assert repeated_pause.json() == paused.json()

    stale_pause = client.post(
        f"/api/governance/evaluations/runs/{queued['id']}/pause",
        headers={"Idempotency-Key": "evaluation-pause-002"},
        json=pause_request,
    )
    assert stale_pause.status_code == 409
    assert stale_pause.json()["detail"]["code"] == "evaluation_version_conflict"

    release.set()
    for _ in range(30):
        current = client.get(f"/api/governance/evaluations/runs/{queued['id']}").json()
        if current["status"] == "paused" and current["completed_cases"] == 1:
            break
    assert current["status"] == "paused"
    original_manifest = current["manifest"]

    before_resume = client.get(f"/api/governance/evaluations/runs/{queued['id']}").json()
    resumed = client.post(
        f"/api/governance/evaluations/runs/{queued['id']}/resume",
        headers={"Idempotency-Key": "evaluation-resume-001"},
        json={"expected_version": before_resume["version"]},
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "completed"
    assert resumed.json()["manifest"] == original_manifest
    assert resumed.json()["completed_cases"] == 1


def test_completed_evaluation_resume_with_stale_version_returns_current_result(client: TestClient):
    from app import evaluation

    completed = evaluation.run_evaluation(
        "core12-v1",
        "deterministic-agent-v1",
        run_mode="smoke",
    )
    assert completed["status"] == "completed"
    stale_version = completed["version"] - 1

    resumed = evaluation.control_evaluation_run(
        completed["id"],
        "resume",
        stale_version,
        "evaluation-resume-after-complete",
    )

    assert resumed is not None
    assert resumed["id"] == completed["id"]
    assert resumed["status"] == "completed"
    assert resumed["version"] == completed["version"]
    assert resumed["completed_cases"] == completed["completed_cases"] == completed["total_cases"]


def test_evaluation_control_requires_idempotency_key(client: TestClient):
    run = client.post(
        "/api/evaluations/runs",
        json={"dataset_version": "core12-v1", "run_mode": "smoke"},
    ).json()
    current = client.get(f"/api/governance/evaluations/runs/{run['id']}").json()
    missing_key = client.post(
        f"/api/governance/evaluations/runs/{run['id']}/pause",
        json={"expected_version": current["version"]},
    )
    assert missing_key.status_code == 400
    assert missing_key.json()["detail"]["code"] == "idempotency_key_required"


def test_evaluation_result_projection_and_human_review_roles_are_separate(client: TestClient):
    run = client.post("/api/evaluations/runs", json={"dataset_version": "core12-v1", "run_mode": "smoke"}).json()
    result_id = run["results"][0]["id"]

    result = client.get(f"/api/governance/evaluations/results/{result_id}")
    assert result.status_code == 200
    result_payload = result.json()
    assert result_payload["run_id"] == run["id"]
    assert result_payload["attempts"]
    assert result_payload["program_assertions"]
    assert "trace_id" in result_payload
    assert result_payload["version"] == 1

    technical = client.post(
        f"/api/governance/evaluations/results/{result_id}/human-review",
        headers={"Idempotency-Key": "evaluation-review-technical-001"},
        json={"review_scope": "technical", "decision": "approve", "note": "运行链路和规则结果已复核", "expected_version": 1},
    )
    assert technical.status_code == 200
    assert technical.json()["review_scope"] == "technical"
    assert technical.json()["reviewer_role"] == "evaluation_engineer"

    repeated_technical = client.post(
        f"/api/governance/evaluations/results/{result_id}/human-review",
        headers={"Idempotency-Key": "evaluation-review-technical-001"},
        json={"review_scope": "technical", "decision": "approve", "note": "重复请求不应新增审核", "expected_version": 1},
    )
    assert repeated_technical.status_code == 200
    assert repeated_technical.json()["id"] == technical.json()["id"]
    after_technical = client.get(f"/api/governance/evaluations/results/{result_id}").json()
    assert after_technical["version"] == 2
    assert len(after_technical["human_reviews"]) == 1

    client.headers.update({"X-CSRF-Token": sign_in_role(client, "business_knowledge_reviewer")})
    business = client.post(
        f"/api/governance/evaluations/results/{result_id}/human-review",
        headers={"Idempotency-Key": "evaluation-review-business-001"},
        json={"review_scope": "business", "decision": "needs_review", "note": "业务事实需要继续核对", "expected_version": 2},
    )
    assert business.status_code == 200
    assert business.json()["reviewer_role"] == "business_knowledge_reviewer"

    reviewed_detail = client.get(f"/api/governance/evaluations/results/{result_id}").json()
    assert {technical.json()["id"], business.json()["id"]}.issubset(set(reviewed_detail["human_review_ids"]))
    assert len(reviewed_detail["human_reviews"]) == 2
    assert reviewed_detail["version"] == 3

    client.headers.update({"X-CSRF-Token": sign_in_role(client, "evaluation_engineer")})
    forbidden = client.post(
        f"/api/governance/evaluations/results/{result_id}/human-review",
        headers={"Idempotency-Key": "evaluation-review-forbidden-001"},
        json={"review_scope": "business", "decision": "approve", "note": "不应代替业务审核", "expected_version": 3},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"]["code"] == "governance_role_forbidden"


def test_human_review_note_and_audit_metadata_redact_token_assignments(client: TestClient):
    run = client.post("/api/evaluations/runs", json={"dataset_version": "core12-v1", "run_mode": "smoke"}).json()
    result_id = run["results"][0]["id"]
    secret = "review-note-secret-123"

    response = client.post(
        f"/api/governance/evaluations/results/{result_id}/human-review",
        headers={"Idempotency-Key": "evaluation-review-redaction-001"},
        json={
            "review_scope": "technical",
            "decision": "approve",
            "note": f"token={secret}",
            "expected_version": 1,
        },
    )

    assert response.status_code == 200
    assert secret not in response.text
    with db.connect() as connection:
        stored_note = connection.execute(
            "SELECT note FROM evaluation_human_reviews WHERE result_id = ?",
            (result_id,),
        ).fetchone()["note"]
        audit_metadata = connection.execute(
            "SELECT metadata FROM audit_events WHERE entity_type = 'evaluation_result' AND entity_id = ? ORDER BY created_at DESC LIMIT 1",
            (result_id,),
        ).fetchone()["metadata"]

    assert secret not in stored_note
    assert secret not in audit_metadata
    assert "token=[REDACTED]" in stored_note


def test_bad_case_repair_note_and_audit_metadata_redact_token_assignments(client: TestClient):
    query = client.post("/api/query", json={"message": "我想查勤工助学岗位"}).json()
    feedback = client.post(
        "/api/feedback",
        json={"response_id": query["feedback_ref"], "rating": "not_helpful", "reason": "需要复核"},
    ).json()
    case_id = feedback["bad_case_id"]
    secret = "repair-note-secret-456"

    response = client.post(
        f"/api/bad-cases/{case_id}/repair",
        json={"repair_note": f"token={secret}"},
    )

    assert response.status_code == 200
    assert secret not in response.text
    with db.connect() as connection:
        row = connection.execute(
            "SELECT repair_note FROM bad_cases WHERE id = ?",
            (case_id,),
        ).fetchone()
        audit_metadata = connection.execute(
            "SELECT metadata FROM audit_events WHERE entity_type = 'bad_case' AND entity_id = ? ORDER BY created_at DESC LIMIT 1",
            (case_id,),
        ).fetchone()["metadata"]

    assert secret not in row["repair_note"]
    assert secret not in audit_metadata
    assert "token=[REDACTED]" in row["repair_note"]


def test_human_review_requires_idempotency_and_rejects_stale_version(client: TestClient):
    run = client.post("/api/evaluations/runs", json={"dataset_version": "core12-v1", "run_mode": "smoke"}).json()
    result_id = run["results"][0]["id"]

    missing_key = client.post(
        f"/api/governance/evaluations/results/{result_id}/human-review",
        json={"review_scope": "technical", "decision": "approve", "note": "缺少幂等键", "expected_version": 1},
    )
    assert missing_key.status_code == 400
    assert missing_key.json()["detail"]["code"] == "idempotency_key_required"

    first = client.post(
        f"/api/governance/evaluations/results/{result_id}/human-review",
        headers={"Idempotency-Key": "evaluation-review-version-001"},
        json={"review_scope": "technical", "decision": "approve", "note": "先写入一条审核", "expected_version": 1},
    )
    assert first.status_code == 200

    stale = client.post(
        f"/api/governance/evaluations/results/{result_id}/human-review",
        headers={"Idempotency-Key": "evaluation-review-version-002"},
        json={"review_scope": "technical", "decision": "approve", "note": "过期版本不得覆盖", "expected_version": 1},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "evaluation_result_version_conflict"
    detail = client.get(f"/api/governance/evaluations/results/{result_id}").json()
    assert detail["version"] == 2
    assert len(detail["human_reviews"]) == 1


def test_evaluation_run_locks_and_exposes_full_version_set(client: TestClient):
    publication = client.get("/api/bootstrap").json()["publication"]
    requested_versions = {
        "dataset_version": "core12-v1",
        "publication_id": publication["id"],
        "model_profile": "deterministic-no-llm-v1",
        "runtime_profile": "deterministic-agent-v1",
        "environment": "mock",
    }

    response = client.post("/api/evaluations/run", json=requested_versions)

    assert response.status_code == 200
    payload = response.json()
    expected_versions = {
        **requested_versions,
        "retrieval_version": publication["retrieval_version"],
        "answer_contract_version": publication["answer_contract_version"],
        "app_version": publication["app_version"],
    }
    assert {key: payload[key] for key in expected_versions} == expected_versions

    listed = next(
        item for item in client.get("/api/evaluations").json()["evaluations"]
        if item["id"] == payload["id"]
    )
    assert {key: listed[key] for key in expected_versions} == expected_versions

    changed = client.post("/api/demo/announcement-change", json={"source_id": "src-venue-v1"}).json()
    published = activate_candidate(client, changed["candidate_revision_id"], "验证历史评测版本不会漂移")
    assert client.get("/api/bootstrap").json()["publication"]["id"] != publication["id"]

    stored = next(
        item for item in client.get("/api/evaluations").json()["evaluations"]
        if item["id"] == payload["id"]
    )
    assert {key: stored[key] for key in expected_versions} == expected_versions
    trace_publications = {
        get_run(case["trace_id"])["context_manifest"]["knowledge_publication_id"]
        for case in payload["cases"]
    }
    assert trace_publications == {publication["id"]}


def test_evaluation_runs_spec_alias_and_retrieval_version_alias(client: TestClient):
    response = client.post("/api/evaluations/runs", json={"dataset_version": "core12-v1"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["retrieval_index_version"] == payload["retrieval_version"]
    assert payload["knowledge_publication_id"] == payload["publication_id"]

    listed = client.get("/api/evaluations/runs").json()["evaluations"]
    assert listed[0]["id"] == payload["id"]
    detail = client.get(f"/api/evaluations/runs/{payload['id']}")
    assert detail.status_code == 200
    assert detail.json()["id"] == payload["id"]

    missing = client.get("/api/evaluations/runs/eval-does-not-exist")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "evaluation_run_not_found"


def test_evaluation_rejects_unimplemented_model_profile(client: TestClient):
    response = client.post(
        "/api/evaluations/run",
        json={"dataset_version": "core12-v1", "model_profile": "external-llm-v1"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "evaluation_model_profile_not_supported"


def test_legacy_evaluation_rows_are_explicitly_unknown_after_migration(tmp_path, monkeypatch):
    database_path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.execute(
        """CREATE TABLE evaluation_runs (
            id TEXT PRIMARY KEY,
            dataset_version TEXT NOT NULL,
            publication_id TEXT NOT NULL,
            runtime_profile TEXT NOT NULL,
            status TEXT NOT NULL,
            summary TEXT NOT NULL,
            cases TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT
        )"""
    )
    connection.execute(
        "INSERT INTO evaluation_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("eval-legacy", "core12-v1", "pub-legacy", "deterministic-agent-v1", "completed", "{}", "[]", "2026-09-15T00:00:00+00:00", "2026-09-15T00:01:00+00:00"),
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(db, "DB_PATH", database_path)
    monkeypatch.setenv("QST_APP_ENV", "development")
    monkeypatch.setenv("QST_GOVERNANCE_BOOTSTRAP_TOKENS", json.dumps(ROLE_TOKENS))

    with TestClient(app) as test_client:
        sign_in_role(test_client, "evaluation_engineer")
        response = test_client.get("/api/evaluations")

    assert response.status_code == 200
    legacy = next(item for item in response.json()["evaluations"] if item["id"] == "eval-legacy")
    assert legacy["publication_id"] == "pub-legacy"
    assert legacy["retrieval_version"] == "legacy/unknown"
    assert legacy["retrieval_index_version"] == "legacy/unknown"
    assert legacy["answer_contract_version"] == "legacy/unknown"
    assert legacy["app_version"] == "legacy/unknown"
    assert legacy["model_profile"] == "legacy/unknown"
    assert legacy["environment"] == "legacy/unknown"


def test_workstudy_provider_lifecycle_and_student_boundary(client: TestClient):
    created = client.post(
        "/api/workstudy/jobs",
        json={
            "actor_type": "demo_provider",
            "title": "图书馆夜间整理助理",
            "department": "图书馆（演示）",
            "location": "示例图书空间",
            "schedule": "周二、周四 18:00-21:00",
            "stipend": "25 元/小时（演示）",
            "qualification": "在校本科生，能稳定安排晚间时间",
            "deadline": "2026-10-15",
            "internal_note": "仅供发布方审核备注，不应暴露给学生",
        },
    )
    assert created.status_code == 200
    job_id = created.json()["id"]
    assert created.json()["status"] == "draft"
    assert created.json()["internal_note"]

    for target_status in ("under_review", "correction_required", "under_review", "published"):
        response = client.post(
            f"/api/workstudy/jobs/{job_id}/transition",
            json={"actor_type": "demo_provider", "target_status": target_status},
        )
        assert response.status_code == 200
        assert response.json()["status"] == target_status

    public_jobs = client.get("/api/workstudy/jobs", params={"actor_type": "demo_student"})
    assert public_jobs.status_code == 200
    public_job = next(job for job in public_jobs.json()["jobs"] if job["id"] == job_id)
    assert "internal_note" not in public_job
    assert public_job["status"] == "published"

    task = client.post(
        "/api/tasks",
        json={"scenario_id": "scenario-workstudy-v1", "form_data": {"job_id": job_id}},
    ).json()
    checked = client.post(
        f"/api/tasks/{task['id']}/precheck",
        json={
            "form_data": {
                "availability": "周二、周四 18:00-21:00",
                "qualification_confirmed": True,
                "materials_confirmed": True,
            }
        },
    ).json()
    assert checked["status"] == "ready_for_preview"


def test_leave_task_supports_approval_correction_and_independent_close(client: TestClient):
    task_id = client.post("/api/tasks", json={"scenario_id": "scenario-leave-v1"}).json()["id"]
    ready = client.post(
        f"/api/tasks/{task_id}/precheck",
        json={
            "form_data": {
                "leave_type": "medical",
                "start_date": "2026-09-20",
                "end_date": "2026-09-21",
                "reason": "身体不适",
                "evidence_confirmed": True,
            }
        },
    ).json()
    assert ready["status"] == "ready_for_preview"
    confirmed = client.post(
        f"/api/tasks/{task_id}/confirm",
        json={"idempotency_key": "leave-confirm-001", "confirmed": True},
    ).json()
    assert confirmed["status"] == "submitted_simulated"

    current = governance_advance(client, task_id, "under_simulated_review", confirmed["version"])
    assert current["status"] == "under_simulated_review"
    current = governance_advance(client, task_id, "correction_required", current["version"])
    assert current["status"] == "correction_required"
    current = governance_advance(client, task_id, "under_simulated_review", current["version"])
    assert current["status"] == "under_simulated_review"
    current = governance_advance(client, task_id, "approved_simulated", current["version"])
    assert current["status"] == "approved_simulated"

    archive_session = client.post(
        "/api/governance/session",
        headers={"X-QST-Governance-Token": "archive-token"},
    )
    assert archive_session.status_code == 200
    client.headers.update({"X-CSRF-Token": archive_session.json()["session"]["csrf_token"]})
    closed = client.post(
        f"/api/tasks/{task_id}/close",
        json={"return_date": "2026-09-22", "confirmed": True},
    )
    assert closed.status_code == 200
    assert closed.json()["status"] == "leave_closed_simulated"
    assert closed.json()["submission_snapshot"]["closure"]["return_date"] == "2026-09-22"
    assert {event["action"] for event in closed.json()["events"]} >= {
        "simulation.confirmed",
        "simulation.status_changed",
        "simulation.leave_closed",
    }


def test_negative_feedback_bad_case_replays_original_trace(client: TestClient):
    query = client.post("/api/query", json={"message": "我想查勤工助学岗位"}).json()
    feedback = client.post(
        "/api/feedback",
        json={
            "response_id": query["feedback_ref"],
            "rating": "not_helpful",
            "reason": "入口不清楚",
        },
    ).json()
    case = next(case for case in client.get("/api/bad-cases").json()["bad_cases"] if case["id"] == feedback["bad_case_id"])
    assert case["input_text"] == "我想查勤工助学岗位"
    assert case["trace_id"]
    assert case["actual"]["trace_id"]
    assert case["expected"]["kind"] == "answer"


def test_evaluation_dataset_metadata_is_consistent_and_each_case_has_one_trace(client: TestClient, monkeypatch):
    from app import evaluation

    calls = []
    original = evaluation.run_query

    def tracked(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(evaluation, "run_query", tracked)
    payload = client.post("/api/evaluations/run", json={"dataset_version": "core12-v1"}).json()
    assert len(calls) == payload["summary"]["total"] == 12
    assert payload["summary"]["freshness_safety_rate"] == 1.0
    assert payload["summary"]["dataset_splits"] == {"golden": 8, "challenge": 3, "holdout": 1}
    assert all(case["trace_id"] for case in payload["cases"])
    assert {case["split"] for case in payload["cases"]} == {"golden", "challenge", "holdout"}
    assert sum(case["split"] == "golden" for case in payload["cases"]) == 8
    assert sum(case["split"] == "challenge" for case in payload["cases"]) == 3
    assert sum(case["split"] == "holdout" for case in payload["cases"]) == 1


def test_announcement_candidate_is_idempotent_and_review_publishes_version(client: TestClient):
    previous = client.get("/api/bootstrap").json()["publication"]["id"]
    changed = client.post(
        "/api/demo/announcement-change",
        json={"source_id": "src-venue-v1", "idempotency_key": "announcement-001"},
    ).json()
    repeated = client.post(
        "/api/demo/announcement-change",
        json={"source_id": "src-venue-v1", "idempotency_key": "announcement-001"},
    ).json()
    assert repeated["candidate_revision_id"] == changed["candidate_revision_id"]
    assert repeated["impact_event_id"] == changed["impact_event_id"]

    payload = activate_candidate(client, changed["candidate_revision_id"], "演示责任方已核对变化范围")
    assert payload["status"] == "published"
    assert payload["publication_id"] != previous
    assert client.get("/api/bootstrap").json()["publication"]["id"] == payload["publication_id"]

    answer = run_query("我想在示例活动广场办迎新活动，应该怎么办？", "current-session", "standalone_web")
    assert answer["response"]["publication_id"] == payload["publication_id"]
    assert answer["response"]["evidence"][0]["revision_id"] == changed["candidate_revision_id"]
    publications = client.get("/api/publications").json()["publications"]
    old = next(item for item in publications if item["id"] == previous)
    assert old["status"] == "superseded"
    assert old["bindings"]["item-venue"] == "src-venue-v1"


def test_business_review_and_activation_are_separate_public_actions(client: TestClient):
    previous = client.get("/api/bootstrap").json()["publication"]["id"]
    changed = client.post("/api/demo/announcement-change", json={"source_id": "src-venue-v1"}).json()

    review_csrf = sign_in_role(client, "business_knowledge_reviewer")
    built = client.post(
        f"/api/source-revisions/{changed['candidate_revision_id']}/review",
        headers={"X-CSRF-Token": review_csrf, "Idempotency-Key": f"source-review-{changed['candidate_revision_id']}"},
        json={"decision": "approve", "review_note": "候选已完成构建", "expected_version": 1},
    )

    assert built.status_code == 200
    candidate = built.json()
    assert candidate["status"] == "awaiting_business_review"
    assert client.get("/api/bootstrap").json()["publication"]["id"] == previous

    business_review = client.post(
        f"/api/publications/{candidate['publication_id']}/business-review",
        headers={"X-CSRF-Token": review_csrf, "Idempotency-Key": f"business-review-{candidate['publication_id']}"},
        json={"decision": "approve", "review_note": "业务事实和适用范围已核对", "expected_version": candidate["version"]},
    )

    assert business_review.status_code == 200
    assert business_review.json()["status"] == "ready"
    assert client.get("/api/bootstrap").json()["publication"]["id"] == previous

    release_run = prepare_release_gate(client, candidate["publication_id"])
    replay_run = client.post(
        "/api/evaluations/runs",
        json={"publication_id": candidate["publication_id"], "run_mode": "replay"},
    )
    assert replay_run.status_code == 200
    assert replay_run.json()["run_mode"] == "replay"
    release_csrf = sign_in_role(client, "release_executor")
    activated = client.post(
        f"/api/publications/{candidate['publication_id']}/activate",
        headers={"X-CSRF-Token": release_csrf, "Idempotency-Key": f"publication-activate-{candidate['publication_id']}"},
        json={"release_note": "发布执行人完成版本切换", "expected_version": business_review.json()["version"]},
    )

    assert activated.status_code == 200
    assert activated.json()["status"] == "published"
    assert client.get("/api/bootstrap").json()["publication"]["id"] == candidate["publication_id"]
    with db.connect() as connection:
        gate = connection.execute(
            "SELECT evaluation_run_id FROM gate_decisions WHERE candidate_publication_id = ? ORDER BY rowid DESC LIMIT 1",
            (candidate["publication_id"],),
        ).fetchone()
    assert gate["evaluation_run_id"] == release_run["id"]


def test_publication_gate_read_does_not_append_a_decision(client: TestClient):
    publication_id = client.get("/api/bootstrap").json()["publication"]["id"]
    sign_in_role(client, "evaluation_engineer")
    first = client.get(f"/api/releases/{publication_id}/gate")
    second = client.get(f"/api/releases/{publication_id}/gate")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["candidate_publication_id"] == publication_id
    with db.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS count FROM gate_decisions WHERE candidate_publication_id = ?",
            (publication_id,),
        ).fetchone()["count"]
    assert count == 0


def test_publication_gate_blocks_when_a_required_gate_has_not_run(client: TestClient):
    publication_id = client.get("/api/bootstrap").json()["publication"]["id"]
    with db.connect() as connection:
        connection.execute("UPDATE bad_cases SET status = 'closed' WHERE status NOT IN ('closed', 'rejected', 'wont_fix')")
        connection.execute(
            "UPDATE publications SET source_gate_result = 'not_run', quality_gate_result = 'pending', responsibility_gate_result = 'pending' WHERE id = ?",
            (publication_id,),
        )

    gate = client.get(f"/api/releases/{publication_id}/gate")

    assert gate.status_code == 200
    assert gate.json()["decision"] == "block"
    assert "不能发布" in gate.json()["decision_reason"]


def test_activation_requires_a_release_gate_decision(client: TestClient):
    previous = client.get("/api/bootstrap").json()["publication"]["id"]
    changed = client.post("/api/demo/announcement-change", json={"source_id": "src-venue-v1"}).json()

    review_csrf = sign_in_role(client, "business_knowledge_reviewer")
    built = client.post(
        f"/api/source-revisions/{changed['candidate_revision_id']}/review",
        headers={"X-CSRF-Token": review_csrf, "Idempotency-Key": f"source-review-{changed['candidate_revision_id']}"},
        json={"decision": "approve", "review_note": "候选构建完成", "expected_version": 1},
    )
    assert built.status_code == 200
    candidate = built.json()

    business_review = client.post(
        f"/api/publications/{candidate['publication_id']}/business-review",
        headers={"X-CSRF-Token": review_csrf, "Idempotency-Key": f"business-review-{candidate['publication_id']}"},
        json={"decision": "approve", "review_note": "业务事实已核对", "expected_version": candidate["version"]},
    )
    assert business_review.status_code == 200

    release_csrf = sign_in_role(client, "release_executor")
    activated = client.post(
        f"/api/publications/{candidate['publication_id']}/activate",
        headers={"X-CSRF-Token": release_csrf, "Idempotency-Key": f"publication-activate-{candidate['publication_id']}"},
        json={"release_note": "未形成发布门决定时不得切换", "expected_version": business_review.json()["version"]},
    )

    assert activated.status_code == 409
    assert activated.json()["detail"]["code"] == "publication_gate_not_released"
    assert client.get("/api/bootstrap").json()["publication"]["id"] == previous
    with db.connect() as connection:
        publication = connection.execute(
            "SELECT status, responsibility_gate_result FROM publications WHERE id = ?",
            (candidate["publication_id"],),
        ).fetchone()
        gate_count = connection.execute(
            "SELECT COUNT(*) AS count FROM gate_decisions WHERE candidate_publication_id = ?",
            (candidate["publication_id"],),
        ).fetchone()["count"]
    assert publication["status"] == "ready"
    assert publication["responsibility_gate_result"] == "pending"
    assert gate_count == 0


def test_activation_rejects_ready_publication_when_keyword_index_is_missing(client: TestClient):
    previous = client.get("/api/bootstrap").json()["publication"]["id"]
    changed = client.post("/api/demo/announcement-change", json={"source_id": "src-venue-v1"}).json()
    review_csrf = sign_in_role(client, "business_knowledge_reviewer")
    built = client.post(
        f"/api/source-revisions/{changed['candidate_revision_id']}/review",
        headers={"X-CSRF-Token": review_csrf, "Idempotency-Key": f"source-review-{changed['candidate_revision_id']}"},
        json={"decision": "approve", "review_note": "候选构建完成", "expected_version": 1},
    ).json()
    publication_id = built["publication_id"]
    reviewed = client.post(
        f"/api/publications/{publication_id}/business-review",
        headers={"X-CSRF-Token": review_csrf, "Idempotency-Key": f"business-review-{publication_id}"},
        json={"decision": "approve", "review_note": "业务事实已核对", "expected_version": built["version"]},
    )
    assert reviewed.status_code == 200

    with db.connect() as connection:
        connection.execute(
            "DELETE FROM knowledge_indexes WHERE publication_id = ? AND index_type = 'keyword'",
            (publication_id,),
        )

    release_csrf = sign_in_role(client, "release_executor")
    activated = client.post(
        f"/api/publications/{publication_id}/activate",
        headers={"X-CSRF-Token": release_csrf, "Idempotency-Key": f"publication-activate-{publication_id}"},
        json={"release_note": "不应绕过索引完整性检查", "expected_version": reviewed.json()["version"]},
    )

    assert activated.status_code == 409
    assert activated.json()["detail"]["code"] == "publication_index_not_ready"
    assert client.get("/api/bootstrap").json()["publication"]["id"] == previous


def test_rejected_candidate_does_not_change_student_publication(client: TestClient):
    previous = client.get("/api/bootstrap").json()["publication"]["id"]
    changed = client.post("/api/demo/announcement-change", json={"source_id": "src-course-v1"}).json()
    review_csrf = sign_in_role(client, "business_knowledge_reviewer")
    rejected = client.post(
        f"/api/source-revisions/{changed['candidate_revision_id']}/review",
        headers={"X-CSRF-Token": review_csrf, "Idempotency-Key": f"source-review-{changed['candidate_revision_id']}"},
        json={"decision": "reject", "review_note": "演示候选缺少责任审核", "expected_version": 1},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert client.get("/api/bootstrap").json()["publication"]["id"] == previous
    answer = run_query("选课什么时候开始？", "current-session", "standalone_web")
    assert answer["response"]["publication_id"] == previous
    assert answer["response"]["evidence"][0]["revision_id"] == "src-course-v1"


def test_publication_rollback_restores_whole_binding_set(client: TestClient, monkeypatch):
    from app.knowledge import get_retrieval_health, load_embedding_profile

    profile = load_embedding_profile({})

    class FakeEmbeddingEncoder:
        @staticmethod
        def _vector(text):
            marker = (
                0 if any(term in text for term in ("示例活动广场", "场地", "活动", "教室", "迎新"))
                else 1 if any(term in text for term in ("选课", "教务", "课表"))
                else 2 if any(term in text for term in ("勤工助学", "岗位"))
                else 3
            )
            vector = [0.0] * profile.dimension
            if marker is not None:
                vector[marker] = 1.0
            return vector

        def encode_documents(self, texts):
            return [self._vector(text) for text in texts]

        def encode_query(self, text):
            if not any(term in text for term in ("示例活动广场", "场地", "活动", "教室", "迎新", "选课", "教务", "课表", "勤工助学", "岗位")):
                return [[0.0] * profile.dimension]
            return [self._vector(text)]

    encoder = FakeEmbeddingEncoder()
    monkeypatch.setattr(
        "app.knowledge.embedding_profile_health",
        lambda: {**profile.to_health_payload(), "status": "ready", "reason": None, "fallback_profile": None},
    )
    monkeypatch.setattr("app.knowledge.load_embedding_encoder", lambda: encoder)

    previous = client.get("/api/bootstrap").json()["publication"]["id"]
    previous_health_before = get_retrieval_health(previous)
    changed = client.post("/api/demo/announcement-change", json={"source_id": "src-venue-v1"}).json()
    published = activate_candidate(client, changed["candidate_revision_id"], "演示发布")
    published_health = get_retrieval_health(published["publication_id"])
    assert published_health["keyword_index"]["status"] == "ready"
    assert published_health["dense_index"]["status"] == "ready"
    assert published_health["dense_index"]["profile_id"] == profile.profile_id
    rollback_csrf = sign_in_role(client, "release_executor")
    rolled_back = client.post(
        f"/api/publications/{published['publication_id']}/rollback",
        headers={"X-CSRF-Token": rollback_csrf, "Idempotency-Key": f"publication-rollback-{published['publication_id']}"},
        json={"reason": "演示回归发现需要回退", "expected_version": published["version"]},
    )
    assert rolled_back.status_code == 200
    assert rolled_back.json()["publication_id"] == previous
    answer = run_query("我想借教室办迎新会", "current-session", "standalone_web")
    assert answer["response"]["publication_id"] == previous
    assert answer["response"]["evidence"][0]["revision_id"] == "src-venue-v1"
    previous_health_after = get_retrieval_health(previous)
    assert previous_health_after["keyword_index"]["status"] == "ready"
    assert previous_health_after["keyword_index"]["build_hash"] == previous_health_before["keyword_index"]["build_hash"]
    assert previous_health_after["dense_index"]["status"] == "ready"
    assert previous_health_after["dense_index"]["profile_id"] == profile.profile_id
    all_publications = client.get("/api/publications").json()["publications"]
    assert any(item["id"] == published["publication_id"] and item["status"] == "superseded" for item in all_publications)


def test_bad_case_requires_regression_before_it_is_verified(client: TestClient):
    query = client.post("/api/query", json={"message": "我想查勤工助学岗位"}).json()
    feedback = client.post(
        "/api/feedback",
        json={
            "response_id": query["feedback_ref"],
            "rating": "not_helpful",
            "reason": "入口不清楚",
        },
    ).json()
    case_id = feedback["bad_case_id"]

    proposed = client.post(
        f"/api/bad-cases/{case_id}/repair",
        json={
            "repair_note": "补充岗位入口说明，并使用原 Trace 回放",
            "candidate_fix_type": "knowledge",
            "candidate_fix_ref": "candidate-revision-work-study-v2",
        },
    )
    assert proposed.status_code == 200
    assert proposed.json()["status"] == "fix_proposed"
    assert proposed.json()["candidate_fix_type"] == "knowledge"
    assert proposed.json()["candidate_fix_ref"] == "candidate-revision-work-study-v2"

    regression = client.post(f"/api/bad-cases/{case_id}/regression")

    assert regression.status_code == 200
    assert regression.json()["status"] == "verified"
    assert regression.json()["regression"]["status"] == "pass"
    assert regression.json()["regression"]["regression_trace_id"]
    case = next(item for item in client.get("/api/bad-cases").json()["bad_cases"] if item["id"] == case_id)
    assert regression.json()["regression"]["original_trace_id"] == case["trace_id"]
    assert regression.json()["regression"]["publication_id"] == case["publication_id"]
    assert case["status"] == "verified"
    assert case["regression_run_id"]
    assert case["publication_id"] == client.get("/api/bootstrap").json()["publication"]["id"]


def test_bad_case_repair_audit_uses_formal_evaluation_role(client: TestClient):
    query = client.post("/api/query", json={"message": "我想查勤工助学岗位"}).json()
    feedback = client.post(
        "/api/feedback",
        json={"response_id": query["feedback_ref"], "rating": "not_helpful", "reason": "审计角色边界"},
    ).json()
    case_id = feedback["bad_case_id"]

    repaired = client.post(
        f"/api/bad-cases/{case_id}/repair",
        json={"repair_note": "记录正式评测角色"},
    )

    assert repaired.status_code == 200
    with db.connect() as connection:
        events = connection.execute(
            "SELECT actor_type FROM audit_events WHERE entity_type = 'bad_case' AND entity_id = ? ORDER BY created_at",
            (case_id,),
        ).fetchall()
    assert events
    assert {row["actor_type"] for row in events} == {"evaluation_engineer"}


def test_bad_case_can_be_closed_only_after_verified_regression(client: TestClient):
    query = client.post("/api/query", json={"message": "我想查勤工助学岗位"}).json()
    feedback = client.post(
        "/api/feedback",
        json={"response_id": query["feedback_ref"], "rating": "not_helpful", "reason": "需要回归"},
    ).json()
    case_id = feedback["bad_case_id"]
    client.post(f"/api/bad-cases/{case_id}/repair", json={"repair_note": "演示修复候选"})
    client.post(f"/api/bad-cases/{case_id}/regression")

    closed = client.post(
        f"/api/bad-cases/{case_id}/close",
        json={"close_note": "同题回归通过，演示关闭"},
    )

    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"


def test_bad_case_failed_regression_remains_a_candidate(client: TestClient):
    query = client.post("/api/query", json={"message": "请告诉我校园卡余额"}).json()
    feedback = client.post(
        "/api/feedback",
        json={"response_id": query["feedback_ref"], "rating": "not_helpful", "reason": "不应编造个人余额"},
    ).json()
    case_id = feedback["bad_case_id"]

    assert client.post(f"/api/bad-cases/{case_id}/repair", json={"repair_note": "补充拒答和官方核验边界"}).json()["status"] == "fix_proposed"
    regression = client.post(f"/api/bad-cases/{case_id}/regression")

    assert regression.status_code == 200
    assert regression.json()["status"] == "fix_proposed"
    assert regression.json()["regression"]["status"] == "fail"
    assert regression.json()["regression"]["publication_id"] == client.get("/api/bootstrap").json()["publication"]["id"]


def test_bad_case_state_transitions_reject_repair_and_close_out_of_order(client: TestClient):
    query = client.post("/api/query", json={"message": "我想查勤工助学岗位"}).json()
    feedback = client.post(
        "/api/feedback",
        json={"response_id": query["feedback_ref"], "rating": "not_helpful", "reason": "状态机边界"},
    ).json()
    case_id = feedback["bad_case_id"]

    early_close = client.post(f"/api/bad-cases/{case_id}/close", json={"close_note": "不能提前关闭"})
    assert early_close.status_code == 409
    assert early_close.json()["detail"]["code"] == "bad_case_close_not_allowed"

    assert client.post(f"/api/bad-cases/{case_id}/repair", json={"repair_note": "候选修复"}).status_code == 200
    repeated_repair = client.post(f"/api/bad-cases/{case_id}/repair", json={"repair_note": "不应覆盖候选"})
    assert repeated_repair.status_code == 409
    assert repeated_repair.json()["detail"]["code"] == "bad_case_repair_not_allowed"


def test_release_gate_exposes_holdout_and_blocks_open_high_severity_cases(client: TestClient):
    publication = client.get("/api/bootstrap").json()["publication"]
    evaluation = client.post("/api/evaluations/runs", json={"dataset_version": "core12-v1"})
    assert evaluation.status_code == 200

    response = client.get(f"/api/releases/{publication['id']}/gate")

    assert response.status_code == 200
    gate = response.json()
    assert gate["candidate_publication_id"] == publication["id"]
    assert gate["evaluation_run_id"] == evaluation.json()["id"]
    assert gate["holdout_summary"] == {"total": 1, "counts": {"pass": 1, "fail": 0, "error": 0, "skip": 0, "unscored": 0}}
    assert gate["p1_open_count"] >= 1
    assert gate["decision"] == "block"
    assert "未关闭" in gate["decision_reason"]


def test_release_gate_reads_validator_and_blocks_hard_validator_failure(client: TestClient):
    publication_id = client.get("/api/bootstrap").json()["publication"]["id"]
    evaluation = prepare_release_gate(client, publication_id)
    with db.connect() as connection:
        result = connection.execute(
            "SELECT id, program_assertions, layer_results FROM evaluation_results WHERE run_id = ? ORDER BY rowid LIMIT 1",
            (evaluation["id"],),
        ).fetchone()
        assertions = json.loads(result["program_assertions"])
        assertions[0]["status"] = "fail"
        connection.execute(
            "UPDATE evaluation_results SET program_assertions = ? WHERE id = ?",
            (json.dumps(assertions, ensure_ascii=False), result["id"]),
        )

    gate = client.get(f"/api/releases/{publication_id}/gate")

    assert gate.status_code == 200
    assert gate.json()["validator_gate"] == "fail"
    assert gate.json()["quality_gate"] == "fail"
    assert gate.json()["decision"] == "block"


def test_release_gate_reads_judge_and_business_review_evidence(client: TestClient):
    publication_id = client.get("/api/bootstrap").json()["publication"]["id"]
    evaluation = prepare_release_gate(client, publication_id)
    with db.connect() as connection:
        result = connection.execute(
            "SELECT id, human_review_ids, layer_results FROM evaluation_results WHERE run_id = ? ORDER BY rowid LIMIT 1",
            (evaluation["id"],),
        ).fetchone()
        layer_results = json.loads(result["layer_results"])
        layer_results["judge"] = {"status": "error", "reason": "judge_timeout"}
        review_id = "review-gate-reject-001"
        connection.execute(
            """INSERT INTO evaluation_human_reviews
               (id, result_id, review_scope, reviewer_role, decision, note, judge_agreement, source_check, created_at)
               VALUES (?, ?, 'business', 'business_knowledge_reviewer', 'reject', ?, 'disagree', 'failed', datetime('now'))""",
            (review_id, result["id"], "业务依据未通过"),
        )
        connection.execute(
            "UPDATE evaluation_results SET layer_results = ?, human_review_ids = ? WHERE id = ?",
            (json.dumps(layer_results, ensure_ascii=False), json.dumps([review_id]), result["id"]),
        )

    gate = client.get(f"/api/releases/{publication_id}/gate")

    assert gate.status_code == 200
    assert gate.json()["judge_gate"] == "error"
    assert gate.json()["human_review_gate"] == "fail"
    assert gate.json()["decision"] == "block"


def test_publication_rollback_accepts_explicit_historical_bundle(client: TestClient):
    original = client.get("/api/bootstrap").json()["publication"]["id"]
    first_change = client.post("/api/demo/announcement-change", json={"source_id": "src-venue-v1"}).json()
    first_publication = activate_candidate(client, first_change["candidate_revision_id"], "第一份演示发布")["publication_id"]
    sign_in_role(client, "evaluation_engineer")
    second_change = client.post("/api/demo/announcement-change", json={"source_id": "src-course-v1"}).json()
    second_publication = activate_candidate(client, second_change["candidate_revision_id"], "第二份演示发布")["publication_id"]

    rollback_csrf = sign_in_role(client, "release_executor")
    rolled_back = client.post(
        f"/api/publications/{first_publication}/rollback",
        headers={"X-CSRF-Token": rollback_csrf, "Idempotency-Key": f"publication-rollback-{first_publication}"},
        json={
            "reason": "恢复已验证的历史完整组合",
            "expected_version": client.get("/api/bootstrap").json()["publication"]["version"],
        },
    )

    assert rolled_back.status_code == 200
    assert rolled_back.json()["publication_id"] == first_publication
    assert rolled_back.json()["rolled_back_from"] == second_publication
    assert client.get("/api/bootstrap").json()["publication"]["id"] == first_publication
    assert client.get("/api/publications/{0}".format(original)).json()["status"] == "superseded"
    with db.connect() as connection:
        rollback_event = connection.execute(
            "SELECT actor_type, metadata FROM audit_events "
            "WHERE action = 'publication.rolled_back' AND entity_id = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (first_publication,),
        ).fetchone()
        transition_rows = connection.execute(
            "SELECT actor_type, metadata FROM audit_events "
            "WHERE action = 'publication.status_changed' AND entity_id IN (?, ?) "
            "ORDER BY rowid DESC LIMIT 2",
            (first_publication, second_publication),
        ).fetchall()

    assert rollback_event["actor_type"] == "release_executor"
    assert json.loads(rollback_event["metadata"])["from"] == second_publication
    rollback_transitions = [
        json.loads(row["metadata"])
        for row in transition_rows
        if "rollback_target" in json.loads(row["metadata"])
        or "rollback_from" in json.loads(row["metadata"])
    ]
    assert len(rollback_transitions) == 2
    assert all(row["actor_type"] == "release_executor" for row in transition_rows)


def test_publication_success_records_explicit_lifecycle_transitions(client: TestClient):
    changed = client.post("/api/demo/announcement-change", json={"source_id": "src-venue-v1"}).json()

    review_csrf = sign_in_role(client, "business_knowledge_reviewer")
    built = client.post(
        f"/api/source-revisions/{changed['candidate_revision_id']}/review",
        headers={"X-CSRF-Token": review_csrf, "Idempotency-Key": f"source-review-{changed['candidate_revision_id']}"},
        json={"decision": "approve", "review_note": "候选构建完成", "expected_version": 1},
    )
    assert built.status_code == 200
    publication_id = built.json()["publication_id"]

    business_review = client.post(
        f"/api/publications/{publication_id}/business-review",
        headers={"X-CSRF-Token": review_csrf, "Idempotency-Key": f"business-review-{publication_id}"},
        json={"decision": "approve", "review_note": "业务事实已核对", "expected_version": built.json()["version"]},
    )
    assert business_review.status_code == 200
    assert business_review.json()["status"] == "ready"

    prepare_release_gate(client, publication_id)
    release_csrf = sign_in_role(client, "release_executor")
    activated = client.post(
        f"/api/publications/{publication_id}/activate",
        headers={"X-CSRF-Token": release_csrf, "Idempotency-Key": f"publication-activate-{publication_id}"},
        json={"release_note": "发布执行人完成切换", "expected_version": business_review.json()["version"]},
    )
    assert activated.status_code == 200
    with db.connect() as connection:
        publication = connection.execute(
            "SELECT status FROM publications WHERE id = ?", (publication_id,)
        ).fetchone()
        transitions = connection.execute(
            "SELECT metadata FROM audit_events "
            "WHERE entity_type = 'publication' AND entity_id = ? "
            "AND action = 'publication.status_changed' ORDER BY created_at",
            (publication_id,),
        ).fetchall()
        claim_statuses = {
            row["claim_status"]
            for row in connection.execute(
                "SELECT DISTINCT claim_status FROM knowledge_claims WHERE publication_id = ?",
                (publication_id,),
            ).fetchall()
        }

    assert publication["status"] == "active"
    assert [json.loads(row["metadata"])["to"] for row in transitions] == [
        "candidate",
        "building",
        "awaiting_business_review",
        "ready",
        "active",
    ]
    assert claim_statuses == {"published"}


def test_publication_build_failure_keeps_previous_active_and_records_failed_candidate(client: TestClient, monkeypatch):
    previous = client.get("/api/bootstrap").json()["publication"]["id"]
    changed = client.post("/api/demo/announcement-change", json={"source_id": "src-venue-v1"}).json()

    def fail_build(*args, **kwargs):
        raise RuntimeError("forced_index_failure")

    monkeypatch.setattr("app.publication.build_keyword_index", fail_build)
    review_csrf = sign_in_role(client, "business_knowledge_reviewer")
    response = client.post(
        f"/api/source-revisions/{changed['candidate_revision_id']}/review",
        headers={"X-CSRF-Token": review_csrf, "Idempotency-Key": f"source-review-{changed['candidate_revision_id']}"},
        json={"decision": "approve", "review_note": "等待索引修复", "expected_version": 1},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "publication_build_failed"
    failed_publication_id = detail["publication_id"]
    assert client.get("/api/bootstrap").json()["publication"]["id"] == previous
    with db.connect() as connection:
        failed = connection.execute(
            "SELECT status, quality_gate_result FROM publications WHERE id = ?",
            (failed_publication_id,),
        ).fetchone()
        current = connection.execute(
            "SELECT id, status FROM publications WHERE id = ?", (previous,)
        ).fetchone()

    assert failed["status"] == "checks_failed"
    assert failed["quality_gate_result"] == "fail"
    assert current["id"] == previous
    assert current["status"] in {"published", "active"}


def test_initial_publication_cannot_roll_back_without_previous_bundle(client: TestClient):
    publication_id = client.get("/api/bootstrap").json()["publication"]["id"]

    rollback_csrf = sign_in_role(client, "release_executor")
    response = client.post(
        f"/api/publications/{publication_id}/rollback",
        headers={"X-CSRF-Token": rollback_csrf, "Idempotency-Key": f"publication-rollback-{publication_id}"},
        json={
            "reason": "没有上一份完整发布组合",
            "expected_version": client.get("/api/bootstrap").json()["publication"]["version"],
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "rollback_target_not_available"


def test_publication_review_requires_idempotency_key(client: TestClient):
    changed = client.post("/api/demo/announcement-change", json={"source_id": "src-venue-v1"}).json()
    csrf_token = sign_in_role(client, "business_knowledge_reviewer")

    response = client.post(
        f"/api/source-revisions/{changed['candidate_revision_id']}/review",
        headers={"X-CSRF-Token": csrf_token},
        json={
            "decision": "approve",
            "review_note": "候选构建完成",
            "expected_version": 1,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "idempotency_key_required"


def test_publication_mutations_replay_and_reject_stale_versions(client: TestClient):
    changed = client.post("/api/demo/announcement-change", json={"source_id": "src-venue-v1"}).json()
    review_csrf = sign_in_role(client, "business_knowledge_reviewer")
    review_headers = {
        "X-CSRF-Token": review_csrf,
        "Idempotency-Key": f"source-review-{changed['candidate_revision_id']}",
    }
    review_body = {
        "decision": "approve",
        "review_note": "候选构建完成",
        "expected_version": 1,
    }
    built = client.post(
        f"/api/source-revisions/{changed['candidate_revision_id']}/review",
        headers=review_headers,
        json=review_body,
    )
    assert built.status_code == 200
    repeated_build = client.post(
        f"/api/source-revisions/{changed['candidate_revision_id']}/review",
        headers=review_headers,
        json=review_body,
    )
    assert repeated_build.status_code == 200
    assert repeated_build.json() == built.json()

    stale_source = client.post(
        f"/api/source-revisions/{changed['candidate_revision_id']}/review",
        headers={**review_headers, "Idempotency-Key": "source-review-stale"},
        json=review_body,
    )
    assert stale_source.status_code == 409
    assert stale_source.json()["detail"]["code"] == "source_revision_version_conflict"

    publication_id = built.json()["publication_id"]
    business_headers = {
        "X-CSRF-Token": review_csrf,
        "Idempotency-Key": f"business-review-{publication_id}",
    }
    business_body = {
        "decision": "approve",
        "review_note": "业务事实已核对",
        "expected_version": built.json()["version"],
    }
    reviewed = client.post(
        f"/api/publications/{publication_id}/business-review",
        headers=business_headers,
        json=business_body,
    )
    assert reviewed.status_code == 200
    repeated_review = client.post(
        f"/api/publications/{publication_id}/business-review",
        headers=business_headers,
        json=business_body,
    )
    assert repeated_review.status_code == 200
    assert repeated_review.json() == reviewed.json()

    stale_business = client.post(
        f"/api/publications/{publication_id}/business-review",
        headers={**business_headers, "Idempotency-Key": "business-review-stale"},
        json=business_body,
    )
    assert stale_business.status_code == 409
    assert stale_business.json()["detail"]["code"] == "publication_version_conflict"

    prepare_release_gate(client, publication_id)
    release_csrf = sign_in_role(client, "release_executor")
    activate_headers = {
        "X-CSRF-Token": release_csrf,
        "Idempotency-Key": f"publication-activate-{publication_id}",
    }
    activate_body = {
        "release_note": "发布执行人完成切换",
        "expected_version": reviewed.json()["version"],
    }
    activated = client.post(
        f"/api/publications/{publication_id}/activate",
        headers=activate_headers,
        json=activate_body,
    )
    assert activated.status_code == 200
    repeated_activation = client.post(
        f"/api/publications/{publication_id}/activate",
        headers=activate_headers,
        json=activate_body,
    )
    assert repeated_activation.status_code == 200
    assert repeated_activation.json() == activated.json()

    stale_activation = client.post(
        f"/api/publications/{publication_id}/activate",
        headers={**activate_headers, "Idempotency-Key": "publication-activate-stale"},
        json=activate_body,
    )
    assert stale_activation.status_code == 409
    assert stale_activation.json()["detail"]["code"] == "publication_version_conflict"

    sign_in_role(client, "evaluation_engineer")
    next_change = client.post("/api/demo/announcement-change", json={"source_id": "src-course-v1"}).json()
    second = activate_candidate(client, next_change["candidate_revision_id"], "第二份完整发布")
    release_csrf = sign_in_role(client, "release_executor")
    rollback_headers = {
        "X-CSRF-Token": release_csrf,
        "Idempotency-Key": f"publication-rollback-{second['publication_id']}",
    }
    rollback_body = {
        "reason": "恢复上一份已验证组合",
        "expected_version": second["version"],
    }
    rolled_back = client.post(
        f"/api/publications/{second['publication_id']}/rollback",
        headers=rollback_headers,
        json=rollback_body,
    )
    assert rolled_back.status_code == 200
    repeated_rollback = client.post(
        f"/api/publications/{second['publication_id']}/rollback",
        headers=rollback_headers,
        json=rollback_body,
    )
    assert repeated_rollback.status_code == 200
    assert repeated_rollback.json() == rolled_back.json()

    stale_rollback = client.post(
        f"/api/publications/{second['publication_id']}/rollback",
        headers={**rollback_headers, "Idempotency-Key": "publication-rollback-stale"},
        json=rollback_body,
    )
    assert stale_rollback.status_code == 409
    assert stale_rollback.json()["detail"]["code"] == "publication_version_conflict"
    assert client.get("/api/bootstrap").json()["publication"]["id"] == rolled_back.json()["publication_id"]
