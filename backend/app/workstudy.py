from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from .db import connect, json_dumps


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _view(row, include_internal: bool = False) -> dict[str, Any]:
    if not row:
        return None
    value = dict(row)
    if not include_internal:
        value.pop("internal_note", None)
        value.pop("created_by", None)
        value.pop("version", None)
    value["simulation"] = True
    value["disclaimer"] = "虚拟岗位 · 演示数据 · 不代表真实招聘或录用结果"
    return value


def get_job(job_id: str, actor_type: str = "demo_student") -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM workstudy_jobs WHERE id = ?", (job_id,)).fetchone()
    return _view(row, actor_type == "demo_provider") if row else None


def list_jobs(actor_type: str = "demo_student") -> list[dict[str, Any]]:
    with connect() as db:
        if actor_type == "demo_provider":
            rows = db.execute("SELECT * FROM workstudy_jobs ORDER BY updated_at DESC").fetchall()
            include_internal = True
        else:
            rows = db.execute("SELECT * FROM workstudy_jobs WHERE status = 'published' ORDER BY deadline, title").fetchall()
            include_internal = False
    return [_view(row, include_internal) for row in rows]


def create_job(data: dict[str, Any]) -> dict[str, Any]:
    job_id = new_id("job")
    now = now_iso()
    with connect() as db:
        db.execute(
            """INSERT INTO workstudy_jobs
            (id, title, department, location, schedule, stipend, qualification, deadline, status,
             created_by, internal_note, version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, 1, ?, ?)""",
            (
                job_id,
                data["title"],
                data["department"],
                data["location"],
                data["schedule"],
                data["stipend"],
                data["qualification"],
                data["deadline"],
                "demo_provider",
                data.get("internal_note", ""),
                now,
                now,
            ),
        )
        db.execute(
            "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (new_id("audit"), "demo_provider", "workstudy.job_created", "workstudy_job", job_id, json_dumps({"simulation": True}), now),
        )
    return get_job(job_id, "demo_provider")


def update_job(job_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
    current = get_job(job_id, "demo_provider")
    if not current:
        return None
    if current["status"] not in {"draft", "correction_required"}:
        raise ValueError("job_update_not_allowed")
    fields = ("title", "department", "location", "schedule", "stipend", "qualification", "deadline", "internal_note")
    values = {field: data[field] for field in fields if field in data and data[field] is not None}
    if not values:
        return current
    assignments = ", ".join(f"{field} = ?" for field in values)
    now = now_iso()
    with connect() as db:
        db.execute(
            f"UPDATE workstudy_jobs SET {assignments}, version = version + 1, updated_at = ? WHERE id = ? AND version = ?",
            (*values.values(), now, job_id, current["version"]),
        )
        db.execute(
            "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (new_id("audit"), "demo_provider", "workstudy.job_updated", "workstudy_job", job_id, json_dumps({"fields": list(values)}), now),
        )
    return get_job(job_id, "demo_provider")


def transition_job(job_id: str, target_status: str) -> dict[str, Any] | None:
    current = get_job(job_id, "demo_provider")
    if not current:
        return None
    allowed = {
        "draft": {"under_review"},
        "under_review": {"correction_required", "published"},
        "correction_required": {"under_review"},
        "published": {"closed"},
    }
    if target_status not in allowed.get(current["status"], set()):
        raise ValueError("job_invalid_transition")
    now = now_iso()
    with connect() as db:
        updated = db.execute(
            "UPDATE workstudy_jobs SET status = ?, version = version + 1, updated_at = ? WHERE id = ? AND version = ?",
            (target_status, now, job_id, current["version"]),
        ).rowcount
        if updated != 1:
            raise ValueError("job_concurrent_update")
        db.execute(
            "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (new_id("audit"), "demo_provider", "workstudy.job_status_changed", "workstudy_job", job_id, json_dumps({"from": current["status"], "to": target_status, "simulation": True}), now),
        )
    return get_job(job_id, "demo_provider")
