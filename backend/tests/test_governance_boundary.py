from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "governance.sqlite3")
    monkeypatch.setenv("QST_APP_ENV", "development")
    monkeypatch.setenv(
        "QST_GOVERNANCE_BOOTSTRAP_TOKENS",
        json.dumps(
            {
                "business_approver": "business-token",
                "site_confirmation_recorder": "site-token",
                "archive_operator": "archive-token",
                "evaluation_engineer": "evaluation-token",
                "business_knowledge_reviewer": "knowledge-token",
                "release_executor": "release-token",
            }
        ),
    )
    with TestClient(app) as test_client:
        yield test_client


def _form() -> dict:
    return {
        "activity_name": "迎新交流会",
        "organization_type": "student_organization",
        "organization_name": "迎新社团",
        "venue": "demo_plaza",
        "activity_date": "2026-10-03",
        "start_time": "10:00",
        "end_time": "12:00",
        "expected_attendees": 80,
        "purpose": "帮助新生熟悉校园",
        "has_sponsor": False,
        "has_performance": False,
        "has_promotion": False,
        "has_supplies": True,
        "materials": {
            "activity_plan": {"purpose": "迎新交流", "audience": "新生", "content": "校园适应分享", "schedule": "09:00-12:00"},
            "activity_flow": {"steps": [{"time": "09:00", "label": "签到"}]},
            "supply_list": {"items": [{"name": "桌牌", "quantity": 10, "purpose": "现场指引"}]},
        },
    }


def _submitted_task(client: TestClient) -> dict:
    csrf = _sign_in(client, "evaluation-token")
    client.headers.update({"X-CSRF-Token": csrf})
    task = client.post("/api/tasks", json={"scenario_id": "scenario-venue-v1"}).json()
    checked = client.post(
        f"/api/tasks/{task['id']}/precheck",
        json={"reference_date": "2026-09-14", "form_data": _form()},
    ).json()
    preview = client.post(f"/api/tasks/{task['id']}/preview").json()["preview"]
    response = client.post(
        f"/api/tasks/{task['id']}/confirm",
        json={
            "idempotency_key": "governance-boundary-confirm",
            "confirmed": True,
            "expected_version": checked["version"],
            "preview_version": preview["preview_version"],
        },
    )
    assert response.status_code == 200
    return response.json()


def _sign_in(client: TestClient, token: str) -> str:
    response = client.post("/api/governance/session", headers={"X-QST-Governance-Token": token})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    return response.json()["session"]["csrf_token"]


def test_governance_session_is_role_bound_and_public_demo_fails_closed(client: TestClient, monkeypatch):
    missing = client.post("/api/governance/session")
    assert missing.status_code == 401

    csrf = _sign_in(client, "business-token")
    session = client.get("/api/governance/session")
    assert session.status_code == 200
    assert session.json()["session"]["role"] == "business_approver"
    assert session.json()["session"]["csrf_token"] != csrf

    monkeypatch.setenv("QST_APP_ENV", "public_demo")
    denied = client.post("/api/governance/session", headers={"X-QST-Governance-Token": "business-token"})
    assert denied.status_code == 401


def test_student_advance_is_blocked_and_governance_session_controls_venue_review(client: TestClient):
    task = _submitted_task(client)
    task_id = task["id"]

    student_attempt = client.post(
        f"/api/tasks/{task_id}/advance",
        json={
            "target_status": "under_simulated_review",
            "expected_version": task["version"],
            "idempotency_key": "student-must-not-review",
        },
    )
    assert student_attempt.status_code == 403
    assert student_attempt.json()["detail"]["code"] == "governance_endpoint_required"

    csrf = _sign_in(client, "business-token")
    missing_csrf = client.post(
        f"/api/governance/tasks/{task_id}/advance",
        json={
            "target_status": "under_simulated_review",
            "expected_version": task["version"],
            "idempotency_key": "missing-csrf",
        },
    )
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["detail"]["code"] == "csrf_invalid"

    advanced = client.post(
        f"/api/governance/tasks/{task_id}/advance",
        headers={"X-CSRF-Token": csrf, "X-Request-ID": "req-governance-venue"},
        json={
            "target_status": "under_simulated_review",
            "reason": "材料进入活动承办审核",
            "expected_version": task["version"],
            "idempotency_key": "governance-venue-review",
        },
    )
    assert advanced.status_code == 200
    value = advanced.json()
    assert value["status"] == "under_simulated_review"
    event = value["events"][-1]
    assert event["metadata"]["actor_role"] == "business_approver"
    assert event["metadata"]["governance_session_id"]
    assert event["metadata"]["request_id"] == "req-governance-venue"

    governance_view = client.get(f"/api/governance/tasks/{task_id}")
    assert governance_view.status_code == 200
    assert governance_view.json()["governance"] == {"role": "business_approver", "session_bound": True}


def test_governance_role_cannot_be_selected_by_target_status(client: TestClient):
    task = _submitted_task(client)
    csrf = _sign_in(client, "business-token")
    denied = client.post(
        f"/api/governance/tasks/{task['id']}/advance",
        headers={"X-CSRF-Token": csrf},
        json={
            "target_status": "archive_pending",
            "reason": "尝试越过现场确认",
            "expected_version": task["version"],
            "idempotency_key": "wrong-governance-role",
        },
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "governance_role_forbidden"
    assert client.get(f"/api/governance/tasks/{task['id']}").json()["task"]["status"] == "submitted_simulated"


def test_student_session_cannot_read_or_start_governance_harness_actions(client: TestClient):
    protected_reads = (
        "/api/evaluations",
        "/api/evaluations/runs",
        "/api/dashboard",
        "/api/bad-cases",
        "/api/sources",
        "/api/publications",
        "/api/releases/pub-demo-2026-09-15/gate",
        "/api/governance/dashboard",
        "/api/governance/bad-cases",
        "/api/governance/releases/pub-demo-2026-09-15/gate",
    )
    for path in protected_reads:
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.json()["detail"]["code"] == "governance_auth_required"

    protected_writes = (
        ("/api/evaluations/run", {"dataset_version": "core12-v1"}),
        ("/api/evaluations/runs", {"dataset_version": "core12-v1"}),
        ("/api/demo/announcement-change", {"source_id": "src-venue-v1"}),
    )
    for path, payload in protected_writes:
        response = client.post(path, json=payload)
        assert response.status_code == 401, path
        assert response.json()["detail"]["code"] == "governance_auth_required"


def test_formal_governance_read_projection_is_available_to_bound_role(client: TestClient):
    csrf = _sign_in(client, "evaluation-token")
    dashboard = client.get("/api/governance/dashboard")
    assert dashboard.status_code == 200
    assert {"latest_evaluation", "bad_cases", "dataset"}.issubset(dashboard.json())

    gate = client.get("/api/governance/releases/pub-demo-2026-09-15/gate")
    assert gate.status_code == 200
    assert {"decision", "fact_source_gate", "quality_gate", "responsibility_gate"}.issubset(gate.json())
    assert csrf


def test_governance_harness_roles_cannot_substitute_for_each_other(client: TestClient):
    evaluation_csrf = _sign_in(client, "evaluation-token")
    review_as_evaluation = client.post(
        "/api/source-revisions/src-venue-v1/review",
        headers={"X-CSRF-Token": evaluation_csrf},
        json={"decision": "approve", "review_note": "不应由评测工程人员代替业务审核", "expected_version": 1},
    )
    assert review_as_evaluation.status_code == 403
    assert review_as_evaluation.json()["detail"]["code"] == "governance_role_forbidden"

    knowledge_csrf = _sign_in(client, "knowledge-token")
    run_as_knowledge = client.post(
        "/api/evaluations/run",
        headers={"X-CSRF-Token": knowledge_csrf},
        json={"dataset_version": "core12-v1"},
    )
    assert run_as_knowledge.status_code == 403
    assert run_as_knowledge.json()["detail"]["code"] == "governance_role_forbidden"

    release_csrf = _sign_in(client, "release-token")
    review_as_release = client.post(
        "/api/publications/pub-demo-2026-09-15/business-review",
        headers={"X-CSRF-Token": release_csrf},
        json={"decision": "approve", "review_note": "不应由发布执行人代替业务知识审核", "expected_version": 1},
    )
    assert review_as_release.status_code == 403
    assert review_as_release.json()["detail"]["code"] == "governance_role_forbidden"
