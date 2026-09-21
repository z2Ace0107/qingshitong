from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app import db
from app.main import app


FORBIDDEN_STUDENT_KEYS = {
    "publication_id",
    "knowledge_publication_id",
    "revision_id",
    "source_revision_id",
    "chunk_id",
    "trace_id",
    "context_manifest",
    "retrieval",
    "intent",
    "provider",
    "model",
    "base_url",
    "api_key",
    "task_id",
}


def _walk_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "projection.sqlite3")
    for name in (
        "QST_LLM_PROVIDER",
        "QST_LLM_BASE_URL",
        "QST_LLM_MODEL",
        "QST_LLM_API_KEY",
        "QST_LLM_API_KEY_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    return TestClient(app)


def test_student_query_is_business_projection_and_feedback_uses_opaque_reference(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/api/query", json={"message": "我想查勤工助学岗位"})

        assert response.status_code == 200
        payload = response.json()
        assert set(payload) == {"response", "feedback_ref"}
        assert payload["feedback_ref"].startswith("feedback-ref-")
        assert "trace_id" not in json.dumps(payload, ensure_ascii=False)
        assert not (FORBIDDEN_STUDENT_KEYS & set(_walk_keys(payload)))

        feedback = client.post(
            "/api/feedback",
            json={"response_id": payload["feedback_ref"], "rating": "not_helpful", "reason": "入口不清楚"},
        )
        assert feedback.status_code == 200
        assert feedback.json()["bad_case_id"]


def test_student_bootstrap_is_a_public_business_projection(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        response = client.get("/api/student/bootstrap")

        assert response.status_code == 200
        payload = response.json()
        assert set(payload) == {"items", "scenarios", "disclaimer"}
        assert payload["items"]
        assert payload["scenarios"]
        assert "当前未接入学校真实业务系统" in payload["disclaimer"]
        serialized = json.dumps(payload, ensure_ascii=False)
        for forbidden in (
            "publication",
            "source_revision_id",
            "source_key",
            "authority_type",
            "dashboard",
            "trace_id",
            "provider",
            "api_key",
        ):
            assert forbidden not in serialized.lower()
        assert "模拟" not in serialized
        assert "演示版" not in serialized
        assert "虚拟责任方" not in serialized


def test_student_query_uses_business_language_for_the_deep_scenario(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/query",
            json={"message": "我想在示例活动广场办迎新活动，10 月 3 日 10 点到 12 点，需要准备什么？"},
        )

        assert response.status_code == 200
        serialized = json.dumps(response.json(), ensure_ascii=False)
        assert "启动模拟任务" not in serialized
        assert "已核验演示依据" not in serialized
        assert "演示版" not in serialized
        assert "虚拟责任方" not in serialized


def test_public_run_replay_and_ready_health_do_not_expose_governance_projection(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        response = client.get("/api/health/ready")
        assert response.status_code == 200
        health = response.json()
        assert health["publication"] in {"ok", "unavailable"}
        assert health["retrieval"] in {"ok", "degraded", "unavailable"}
        assert health["provider_status"] in {"configured", "unavailable", "not_configured"}
        assert "api_key" not in json.dumps(health, ensure_ascii=False).lower()
        assert "authorization" not in json.dumps(health, ensure_ascii=False).lower()
        assert "publication_id" not in json.dumps(health, ensure_ascii=False).lower()

        replay = client.get("/api/runs/trace-not-for-students")
        assert replay.status_code == 403
        assert replay.json()["detail"]["code"] == "governance_auth_required"


def test_legacy_task_routes_require_governance_projection_boundary(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        legacy = client.get("/api/tasks")

        assert legacy.status_code == 401
        assert legacy.json()["detail"]["code"] == "governance_auth_required"


def test_student_task_projection_keeps_business_fields_and_hides_internal_record(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/student/tasks",
            json={"scenario_id": "scenario-venue-v1", "channel": "standalone_web"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert set(payload) >= {"task_ref", "status", "scenario_title", "fields", "materials", "next_actions"}
        assert payload["task_ref"]["route"].startswith("/student/tasks/")
        assert payload["status"] == "draft"
        assert payload["scenario_title"] == "活动场地申请"
        assert payload["materials"]
        assert "publication_id" not in json.dumps(payload, ensure_ascii=False)
        assert "source_snapshot_id" not in json.dumps(payload, ensure_ascii=False)
        assert "actor_type" not in json.dumps(payload, ensure_ascii=False)
        assert "events" not in payload
        assert "submission_history" not in payload


def test_student_task_precheck_returns_projected_business_status(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        created = client.post(
            "/api/student/tasks",
            json={"scenario_id": "scenario-venue-v1"},
        ).json()
        task_id = created["task_ref"]["task_id"]
        checked = client.post(
            f"/api/student/tasks/{task_id}/precheck",
            json={"form_data": {"activity_name": "迎新交流会"}},
        )

        assert checked.status_code == 200
        payload = checked.json()
        assert payload["status"] == "precheck_failed"
        assert any(field["key"] == "activity_date" for field in payload["fields"])
        assert any(item["status"] == "not_started" for item in payload["materials"])
        assert "publication_id" not in json.dumps(payload, ensure_ascii=False)
