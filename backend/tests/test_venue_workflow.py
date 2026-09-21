from __future__ import annotations

from datetime import date

import pytest
import json
from fastapi.testclient import TestClient

from app import db
from app.main import app


ROLE_TOKENS = {
    "evaluation_engineer": "evaluation-token",
    "business_knowledge_reviewer": "knowledge-token",
    "release_executor": "release-token",
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "venue.sqlite3")
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
        session = test_client.post(
            "/api/governance/session",
            headers={"X-QST-Governance-Token": ROLE_TOKENS["evaluation_engineer"]},
        )
        assert session.status_code == 200
        test_client.headers.update({"X-CSRF-Token": session.json()["session"]["csrf_token"]})
        yield test_client


def _sign_in_role(client: TestClient, role: str) -> str:
    response = client.post("/api/governance/session", headers={"X-QST-Governance-Token": ROLE_TOKENS[role]})
    assert response.status_code == 200
    csrf_token = response.json()["session"]["csrf_token"]
    client.headers.update({"X-CSRF-Token": csrf_token})
    return csrf_token


def _materials() -> dict:
    return {
        "activity_plan": {
            "purpose": "迎新交流",
            "audience": "新生",
            "content": "校园适应分享",
            "schedule": "09:00-12:00",
        },
        "activity_flow": {
            "steps": [
                {"time": "09:00", "label": "签到"},
                {"time": "09:30", "label": "交流"},
            ]
        },
        "supply_list": {
            "items": [{"name": "桌牌", "quantity": 10, "purpose": "现场指引"}],
        },
    }


def _form(activity_date: str) -> dict:
    return {
        "activity_name": "迎新交流会",
        "organization_type": "student_organization",
        "organization_name": "迎新社团",
        "venue": "demo_plaza",
        "activity_date": activity_date,
        "start_time": "10:00",
        "end_time": "12:00",
        "expected_attendees": 80,
        "purpose": "帮助新生熟悉校园",
        "has_sponsor": False,
        "has_performance": False,
        "has_promotion": False,
        "has_supplies": True,
        "materials": _materials(),
    }


def _create_task(client: TestClient) -> str:
    response = client.post("/api/tasks", json={"scenario_id": "scenario-venue-v1"})
    assert response.status_code == 200
    return response.json()["id"]


def _advance(client: TestClient, task_id: str, payload: dict) -> TestClient:
    role_tokens = {
        "under_simulated_review": "business-token",
        "correction_required": "business-token",
        "rejected_simulated": "business-token",
        "approved_simulated": "business-token",
        "awaiting_offline_confirmation": "business-token",
        "archive_pending": "site-token",
        "completed_simulated": "archive-token",
        "leave_closed_simulated": "archive-token",
    }
    session = client.post(
        "/api/governance/session",
        headers={"X-QST-Governance-Token": role_tokens[payload["target_status"]]},
    )
    assert session.status_code == 200
    client.headers.update({"X-CSRF-Token": session.json()["session"]["csrf_token"]})
    response = client.post(
        f"/api/governance/tasks/{task_id}/advance",
        headers={"X-CSRF-Token": session.json()["session"]["csrf_token"]},
        json=payload,
    )
    return response


def test_venue_precheck_uses_ten_business_days_and_weekend_opening_rules(client: TestClient):
    task_id = _create_task(client)

    too_soon = client.post(
        f"/api/tasks/{task_id}/precheck",
        json={"reference_date": "2026-09-14", "form_data": _form("2026-09-25")},
    )

    assert too_soon.status_code == 200
    blocked = too_soon.json()
    assert blocked["status"] == "precheck_failed"
    assert any(item["rule_id"] == "venue-lead-time-10-business-days" and not item["passed"] for item in blocked["rule_results"])
    assert any(item["rule_id"] == "venue-open-days" and item["passed"] for item in blocked["rule_results"])

    valid = client.post(
        f"/api/tasks/{task_id}/precheck",
        json={"reference_date": "2026-09-14", "form_data": _form("2026-10-03")},
    )

    assert valid.status_code == 200
    checked = valid.json()
    assert checked["status"] == "ready_for_preview"
    assert all(item["passed"] for item in checked["rule_results"])


@pytest.mark.parametrize(
    ("activity_date", "start_time", "end_time", "availability", "passed"),
    [
        ("2026-10-03", "10:00", "12:00", "available", True),
        ("2026-10-10", "10:00", "12:00", "conflict", False),
        ("2026-10-03", "07:00", "08:00", "blocked_by_rule", False),
        ("2026-09-25", "10:00", "12:00", "blocked_by_rule", False),
    ],
)
def test_venue_virtual_slot_fixture_is_deterministic(
    client: TestClient,
    activity_date: str,
    start_time: str,
    end_time: str,
    availability: str,
    passed: bool,
):
    task_id = _create_task(client)
    form = _form(activity_date)
    form["start_time"] = start_time
    form["end_time"] = end_time
    checked = client.post(
        f"/api/tasks/{task_id}/precheck",
        json={"reference_date": "2026-09-14", "form_data": form},
    )

    assert checked.status_code == 200
    slot = next(item for item in checked.json()["rule_results"] if item["rule_id"] == "venue-virtual-slot")
    assert slot["availability"] == availability
    assert slot["passed"] is passed
    assert slot["provenance"] == "project_rule"
    if availability == "conflict":
        assert slot["occupied_label"]
        assert slot["alternatives"]


def test_venue_material_records_are_structured_and_sponsor_material_is_conditional(client: TestClient):
    task_id = _create_task(client)
    form = _form("2026-10-03")
    form["has_sponsor"] = True

    response = client.post(
        f"/api/tasks/{task_id}/precheck",
        json={"reference_date": "2026-09-14", "form_data": form},
    )

    assert response.status_code == 200
    checked = response.json()
    sponsor = next(item for item in checked["material_results"] if item["material_id"] == "sponsor_agreement")
    assert sponsor["applicability"] == "required"
    assert sponsor["provenance"] == "virtual_design"
    assert sponsor["status"] == "not_started"
    assert sponsor["missing_fields"]
    assert checked["status"] == "precheck_failed"

    form["materials"]["sponsor_agreement"] = {
        "counterparty": "校内活动合作方",
        "scope": "活动物资支持",
        "terms_confirmed": True,
    }
    ready = client.post(
        f"/api/tasks/{task_id}/precheck",
        json={"reference_date": "2026-09-14", "form_data": form},
    )

    assert ready.status_code == 200
    sponsor = next(item for item in ready.json()["material_results"] if item["material_id"] == "sponsor_agreement")
    assert sponsor["status"] == "ready"
    assert sponsor["missing_fields"] == []
    assert ready.json()["status"] == "ready_for_preview"


def test_venue_material_workspace_saves_structured_content_with_expected_version(client: TestClient):
    task_id = _create_task(client)
    initial = client.get(f"/api/tasks/{task_id}").json()
    materials = {
        "activity_plan": {"purpose": "迎新交流"},
        "activity_flow": {"steps": [{"time": "09:00", "label": "签到"}]},
    }

    saved = client.put(
        f"/api/tasks/{task_id}/materials",
        json={"materials": materials, "expected_version": initial["version"]},
    )

    assert saved.status_code == 200
    task = saved.json()
    assert task["status"] == "collecting"
    assert task["form_data"]["materials"] == materials
    plan = next(item for item in task["material_results"] if item["material_id"] == "activity_plan")
    assert plan["status"] == "editing"
    assert plan["missing_fields"] == ["audience", "content", "schedule"]

    conflict = client.put(
        f"/api/tasks/{task_id}/materials",
        json={"materials": materials, "expected_version": initial["version"]},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "task_concurrent_update"


def test_venue_preview_version_is_required_for_confirmation_and_expires_after_edit(client: TestClient):
    task_id = _create_task(client)
    checked = client.post(
        f"/api/tasks/{task_id}/precheck",
        json={"reference_date": "2026-09-14", "form_data": _form("2026-10-03")},
    ).json()
    preview = client.post(f"/api/tasks/{task_id}/preview")

    assert preview.status_code == 200
    preview_payload = preview.json()
    preview_version = preview_payload["preview"]["preview_version"]
    assert preview_version == checked["version"]
    assert preview_payload["task"]["status"] == "ready_for_preview"

    edited = client.put(
        f"/api/tasks/{task_id}/materials",
        json={
            "materials": {"activity_plan": {"purpose": "修改后的活动目标"}},
            "expected_version": checked["version"],
        },
    )
    assert edited.status_code == 200

    expired = client.post(
        f"/api/tasks/{task_id}/confirm",
        json={
            "idempotency_key": "venue-expired-preview",
            "confirmed": True,
            "expected_version": preview_version,
            "preview_version": preview_version,
        },
    )
    assert expired.status_code == 409
    assert expired.json()["detail"]["code"] == "preview_expired"


def test_venue_review_chain_records_business_nodes_and_is_idempotent(client: TestClient):
    task_id = _create_task(client)
    checked = client.post(
        f"/api/tasks/{task_id}/precheck",
        json={"reference_date": "2026-09-14", "form_data": _form("2026-10-03")},
    ).json()
    preview = client.post(f"/api/tasks/{task_id}/preview").json()["preview"]
    submitted = client.post(
        f"/api/tasks/{task_id}/confirm",
        json={
            "idempotency_key": "venue-review-confirm",
            "confirmed": True,
            "expected_version": checked["version"],
            "preview_version": preview["preview_version"],
        },
    ).json()

    reviewed = _advance(client, task_id, {
            "target_status": "under_simulated_review",
            "reason": "活动承办审核开始",
            "expected_version": submitted["version"],
            "idempotency_key": "venue-review-start",
        })
    assert reviewed.status_code == 200
    review_task = reviewed.json()
    assert review_task["status"] == "under_simulated_review"
    review_event = review_task["events"][-1]
    assert review_event["metadata"]["actor_role"] == "business_approver"
    assert review_event["metadata"]["reason"] == "活动承办审核开始"

    repeated = _advance(client, task_id, {
            "target_status": "under_simulated_review",
            "reason": "重复请求",
            "expected_version": submitted["version"],
            "idempotency_key": "venue-review-start",
        })
    assert repeated.status_code == 200
    assert len(repeated.json()["events"]) == len(review_task["events"])

    current = repeated.json()
    for target_status, role in (
        ("awaiting_offline_confirmation", "business_approver"),
        ("archive_pending", "site_confirmation_recorder"),
        ("completed_simulated", "archive_operator"),
    ):
        response = _advance(client, task_id, {
                "target_status": target_status,
                "reason": f"推进到 {target_status}",
                "expected_version": current["version"],
                "idempotency_key": f"venue-review-{target_status}",
            })
        assert response.status_code == 200
        current = response.json()
        assert current["status"] == target_status
        assert current["events"][-1]["metadata"]["actor_role"] == role


def test_venue_student_review_nodes_expose_business_next_step(client: TestClient):
    task_id = _create_task(client)
    checked = client.post(
        f"/api/tasks/{task_id}/precheck",
        json={"reference_date": "2026-09-14", "form_data": _form("2026-10-03")},
    ).json()
    preview = client.post(f"/api/tasks/{task_id}/preview").json()["preview"]
    submitted = client.post(
        f"/api/tasks/{task_id}/confirm",
        json={
            "idempotency_key": "venue-student-next-step",
            "confirmed": True,
            "expected_version": checked["version"],
            "preview_version": preview["preview_version"],
        },
    ).json()

    current = _advance(
        client,
        task_id,
        {
            "target_status": "under_simulated_review",
            "reason": "活动承办审核开始",
            "expected_version": submitted["version"],
            "idempotency_key": "venue-student-next-step-review",
        },
    ).json()
    student = client.get(f"/api/student/tasks/{task_id}").json()
    assert student["review_nodes"][-1]["next_step"] == "等待活动承办审核处理"

    current = _advance(
        client,
        task_id,
        {
            "target_status": "awaiting_offline_confirmation",
            "reason": "活动承办审核已完成",
            "expected_version": current["version"],
            "idempotency_key": "venue-student-next-step-site",
        },
    ).json()
    student = client.get(f"/api/student/tasks/{task_id}").json()
    assert student["review_nodes"][-1]["next_step"] == "按当前办理提示完成现场确认"


def test_venue_correction_creates_new_submission_version_and_preserves_history(client: TestClient):
    task_id = _create_task(client)
    original_form = _form("2026-10-03")
    checked = client.post(
        f"/api/tasks/{task_id}/precheck",
        json={"reference_date": "2026-09-14", "form_data": original_form},
    ).json()
    preview = client.post(f"/api/tasks/{task_id}/preview").json()["preview"]
    submitted = client.post(
        f"/api/tasks/{task_id}/confirm",
        json={
            "idempotency_key": "venue-submission-v1",
            "confirmed": True,
            "expected_version": checked["version"],
            "preview_version": preview["preview_version"],
        },
    ).json()

    under_review = _advance(client, task_id, {
            "target_status": "under_simulated_review",
            "reason": "开始核对材料",
            "expected_version": submitted["version"],
            "idempotency_key": "venue-correction-review",
        }).json()
    correction = _advance(client, task_id, {
            "target_status": "correction_required",
            "reason": "活动流程需要补充",
            "expected_version": under_review["version"],
            "idempotency_key": "venue-correction-required",
        }).json()
    assert correction["status"] == "correction_required"
    assert correction["submission_history"][0]["submission_version"] == 1
    assert correction["submission_history"][0]["status"] == "correction_required"
    assert correction["submission_history"][0]["form_data"]["activity_name"] == original_form["activity_name"]

    _sign_in_role(client, "evaluation_engineer")
    revised_form = _form("2026-10-11")
    revised_form["purpose"] = "补充活动流程后的迎新交流"
    revised_checked = client.post(
        f"/api/tasks/{task_id}/precheck",
        json={"reference_date": "2026-09-14", "form_data": revised_form},
    ).json()
    revised_preview = client.post(f"/api/tasks/{task_id}/preview").json()["preview"]
    revised = client.post(
        f"/api/tasks/{task_id}/confirm",
        json={
            "idempotency_key": "venue-submission-v2",
            "confirmed": True,
            "expected_version": revised_checked["version"],
            "preview_version": revised_preview["preview_version"],
        },
    )

    assert revised.status_code == 200
    current = revised.json()
    assert current["status"] == "submitted_simulated"
    assert current["submission_snapshot"]["submission_version"] == 2
    assert [item["submission_version"] for item in current["submission_history"]] == [1, 2]
    assert current["submission_history"][0]["form_data"]["activity_date"] == "2026-10-03"
    assert current["submission_history"][1]["form_data"]["activity_date"] == "2026-10-11"


def test_venue_reconfirmation_exposes_change_and_requires_explicit_confirmation(client: TestClient):
    task_id = _create_task(client)
    changed = client.post(
        "/api/demo/announcement-change",
        json={"source_id": "src-venue-v1", "idempotency_key": "venue-reconfirm-change"},
    ).json()

    before_publish = client.get(f"/api/tasks/{task_id}").json()
    assert before_publish["status"] == "draft"
    assert changed["affected_tasks"] == []

    review_csrf = _sign_in_role(client, "business_knowledge_reviewer")
    activated = client.post(
        f"/api/source-revisions/{changed['candidate_revision_id']}/review",
        headers={"X-CSRF-Token": review_csrf, "Idempotency-Key": f"source-review-{changed['candidate_revision_id']}"},
        json={"decision": "approve", "review_note": "候选内容已构建", "expected_version": 1},
    ).json()
    reviewed = client.post(
        f"/api/publications/{activated['publication_id']}/business-review",
        headers={"X-CSRF-Token": review_csrf, "Idempotency-Key": f"business-review-{activated['publication_id']}"},
        json={"decision": "approve", "review_note": "业务依据已核对", "expected_version": activated["version"]},
    )
    assert reviewed.status_code == 200
    _sign_in_role(client, "evaluation_engineer")
    with db.connect() as connection:
        connection.execute(
            "UPDATE bad_cases SET status = 'closed', close_note = ?, updated_at = datetime('now') "
            "WHERE id = 'bc-seed-venue-confusion'",
            ("测试夹具已隔离种子阻断案例",),
        )
    evaluation = client.post(
        "/api/evaluations/runs",
        json={"publication_id": activated["publication_id"], "run_mode": "release"},
    )
    assert evaluation.status_code == 200
    assert all(case["status"] == "pass" for case in evaluation.json()["cases"]), [
        (case["case_id"], case["status"], case.get("failure_categories"))
        for case in evaluation.json()["cases"]
        if case["status"] != "pass"
    ]
    release_csrf = _sign_in_role(client, "release_executor")
    published = client.post(
        f"/api/publications/{activated['publication_id']}/activate",
        headers={"X-CSRF-Token": release_csrf, "Idempotency-Key": f"publication-activate-{activated['publication_id']}"},
        json={"release_note": "切换已审核依据", "expected_version": reviewed.json()["version"]},
    )
    assert published.status_code == 200

    impacted = client.get(f"/api/tasks/{task_id}").json()
    assert impacted["status"] == "needs_reconfirmation"
    current_version = impacted["version"]
    view = client.get(
        f"/api/tasks/{task_id}/reconfirmation",
        params={"impact_event_id": changed["impact_event_id"]},
    )
    assert view.status_code == 200
    payload = view.json()
    assert payload["action"]["code"] == "reconfirm"
    assert payload["old_basis"]["source_revision_id"] != payload["new_basis"]["source_revision_id"]
    assert payload["saved_materials"] == impacted["material_results"]

    kept = client.post(
        f"/api/tasks/{task_id}/reconfirmation",
        json={
            "impact_event_id": changed["impact_event_id"],
            "idempotency_key": "venue-reconfirm-keep",
            "confirmed": False,
            "expected_version": current_version,
        },
    )
    assert kept.status_code == 200
    assert kept.json()["status"] == "needs_reconfirmation"

    confirmed = client.post(
        f"/api/tasks/{task_id}/reconfirmation",
        json={
            "impact_event_id": changed["impact_event_id"],
            "idempotency_key": "venue-reconfirm-confirm",
            "confirmed": True,
            "expected_version": current_version,
        },
    )
    assert confirmed.status_code == 200
    refreshed = confirmed.json()
    assert refreshed["status"] == "collecting"
    assert refreshed["source_snapshot_id"] == changed["candidate_revision_id"]
    assert refreshed["submission_snapshot"] is None

    repeated = client.post(
        f"/api/tasks/{task_id}/reconfirmation",
        json={
            "impact_event_id": changed["impact_event_id"],
            "idempotency_key": "venue-reconfirm-confirm",
            "confirmed": True,
            "expected_version": current_version,
        },
    )
    assert repeated.status_code == 200
    assert repeated.json() == refreshed
    assert sum(event["action"] == "simulation.reconfirmed" for event in repeated.json()["events"]) == 1


def test_venue_student_reconfirmation_projection_hides_internal_change_ids(client: TestClient):
    task_id = _create_task(client)
    changed = client.post(
        "/api/demo/announcement-change",
        json={"source_id": "src-venue-v1", "idempotency_key": "student-reconfirm-change"},
    ).json()

    review_csrf = _sign_in_role(client, "business_knowledge_reviewer")
    activated = client.post(
        f"/api/source-revisions/{changed['candidate_revision_id']}/review",
        headers={"X-CSRF-Token": review_csrf, "Idempotency-Key": f"student-source-review-{changed['candidate_revision_id']}"},
        json={"decision": "approve", "review_note": "候选内容已构建", "expected_version": 1},
    ).json()
    reviewed = client.post(
        f"/api/publications/{activated['publication_id']}/business-review",
        headers={"X-CSRF-Token": review_csrf, "Idempotency-Key": f"student-business-review-{activated['publication_id']}"},
        json={"decision": "approve", "review_note": "业务依据已核对", "expected_version": activated["version"]},
    )
    assert reviewed.status_code == 200

    _sign_in_role(client, "evaluation_engineer")
    with db.connect() as connection:
        connection.execute(
            "UPDATE bad_cases SET status = 'closed', close_note = ?, updated_at = datetime('now') "
            "WHERE id = 'bc-seed-venue-confusion'",
            ("测试夹具已隔离种子阻断案例",),
        )
    evaluation = client.post(
        "/api/evaluations/runs",
        json={"publication_id": activated["publication_id"], "run_mode": "release"},
    )
    assert evaluation.status_code == 200

    release_csrf = _sign_in_role(client, "release_executor")
    published = client.post(
        f"/api/publications/{activated['publication_id']}/activate",
        headers={"X-CSRF-Token": release_csrf, "Idempotency-Key": f"student-publication-activate-{activated['publication_id']}"},
        json={"release_note": "切换已审核依据", "expected_version": reviewed.json()["version"]},
    )
    assert published.status_code == 200

    response = client.get(f"/api/student/tasks/{task_id}/reconfirmation")
    assert response.status_code == 200
    payload = response.json()
    serialized = json.dumps(payload, ensure_ascii=False)
    assert payload["status"] == "needs_reconfirmation"
    assert payload["action"]["code"] == "reconfirm"
    assert payload["change_summary"]
    assert payload["old_basis"]["title"]
    assert payload["new_basis"]["title"]
    assert payload["saved_materials"]
    for forbidden in ("impact_event_id", "publication_id", "source_revision_id", "task_id", "submission_history"):
        assert forbidden not in serialized

    confirmed = client.post(
        f"/api/student/tasks/{task_id}/reconfirmation",
        headers={"Idempotency-Key": "student-reconfirm-confirm"},
        json={"confirmed": True, "expected_version": payload["version"]},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "collecting"

    repeated = client.post(
        f"/api/student/tasks/{task_id}/reconfirmation",
        headers={"Idempotency-Key": "student-reconfirm-confirm"},
        json={"confirmed": True, "expected_version": payload["version"]},
    )
    assert repeated.status_code == 200
    assert repeated.json() == confirmed.json()
