from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .db import connect, json_dumps, json_loads
from .knowledge import get_current_publication, get_scenario
from .workstudy import get_job


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _add_business_days(start: date, days: int) -> date:
    cursor = start
    remaining = days
    while remaining:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            remaining -= 1
    return cursor


def _conditional_material_result(rule: dict[str, Any], form: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    selected = rule.get("branches", {}).get(str(form.get(rule.get("field", ""))))
    if not selected:
        return {
            "rule_id": rule["id"],
            "name": "请先选择请假类型",
            "status": "待选择",
            "required": True,
            "detail": "不同请假类型需要检查不同证明材料",
        }, False
    confirmed = bool(form.get(rule.get("material_field", "")))
    return {
        "rule_id": rule["id"],
        "name": selected["name"],
        "status": "已确认（演示）" if confirmed else "待确认",
        "required": True,
        "detail": selected["detail"],
    }, confirmed


_VENUE_MATERIAL_DEFINITIONS = (
    {
        "material_id": "venue_application",
        "label": "活动场地申请表",
        "applicability": "required",
        "provenance": "observed_rewritten",
        "required_fields": ("activity_name", "organization_name", "venue", "activity_date", "start_time", "end_time"),
    },
    {
        "material_id": "activity_plan",
        "label": "活动策划书",
        "applicability": "required",
        "provenance": "observed_rewritten",
        "required_fields": ("purpose", "audience", "content", "schedule"),
    },
    {
        "material_id": "activity_flow",
        "label": "活动流程",
        "applicability": "required",
        "provenance": "project_rule",
        "required_fields": ("steps",),
    },
    {
        "material_id": "supply_list",
        "label": "物资清单",
        "applicability": "conditional",
        "trigger": "has_supplies",
        "provenance": "project_rule",
        "required_fields": ("items",),
    },
    {
        "material_id": "program_list",
        "label": "节目单",
        "applicability": "conditional",
        "trigger": "has_performance",
        "provenance": "virtual_design",
        "required_fields": ("items",),
    },
    {
        "material_id": "promotional_design",
        "label": "海报、横幅样式",
        "applicability": "conditional",
        "trigger": "has_promotion",
        "provenance": "virtual_design",
        "required_fields": ("text", "spec"),
    },
    {
        "material_id": "sponsor_agreement",
        "label": "商家协议",
        "applicability": "conditional",
        "trigger": "has_sponsor",
        "provenance": "virtual_design",
        "required_fields": ("counterparty", "scope", "terms_confirmed"),
    },
)


_VENUE_VIRTUAL_SLOT_FIXTURES = (
    {
        "slot_id": "slot-venue-available",
        "date": "2026-10-03",
        "start_time": "10:00",
        "end_time": "12:00",
        "availability": "available",
        "occupied_label": None,
        "alternatives": [],
    },
    {
        "slot_id": "slot-venue-conflict",
        "date": "2026-10-10",
        "start_time": "10:00",
        "end_time": "12:00",
        "availability": "conflict",
        "occupied_label": "校级迎新活动（场景 fixture）",
        "alternatives": ["2026-10-10 14:00-16:00"],
    },
    {
        "slot_id": "slot-venue-blocked-opening",
        "date": "2026-10-03",
        "start_time": "07:00",
        "end_time": "08:00",
        "availability": "blocked_by_rule",
        "occupied_label": None,
        "alternatives": ["2026-10-03 08:00-10:00"],
    },
    {
        "slot_id": "slot-venue-blocked-lead-time",
        "date": "2026-09-25",
        "start_time": "10:00",
        "end_time": "12:00",
        "availability": "blocked_by_rule",
        "occupied_label": None,
        "alternatives": ["2026-10-03 10:00-12:00"],
    },
)


def _canonical_venue_form(form: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Normalize the confirmed virtual venue contract while keeping demo aliases readable."""
    legacy = "activity_name" not in form and any(key in form for key in ("event_name", "date", "attendees", "organizer"))
    canonical = dict(form)
    aliases = {
        "activity_name": form.get("event_name"),
        "organization_type": "student_organization" if form.get("organizer") else None,
        "organization_name": form.get("organizer"),
        "venue": "demo_plaza",
        "activity_date": form.get("date"),
        "expected_attendees": form.get("attendees"),
        "purpose": form.get("event_name"),
    }
    for key, value in aliases.items():
        if not canonical.get(key) and value is not None:
            canonical[key] = value
    if legacy:
        canonical.setdefault("has_sponsor", False)
        canonical.setdefault("has_supplies", False)
        canonical.setdefault("has_performance", False)
        canonical.setdefault("has_promotion", False)
    return canonical, legacy


def _venue_value_present(value: Any) -> bool:
    return value is not None and value is not False and value != ""


def _material_content_complete(content: Any, required_fields: tuple[str, ...]) -> tuple[str, list[str]]:
    if not isinstance(content, dict):
        return "not_started", list(required_fields)
    missing = [field for field in required_fields if not _venue_value_present(content.get(field))]
    if not missing:
        return "ready", []
    present_count = len(required_fields) - len(missing)
    return ("editing" if present_count else "not_started"), missing


def _venue_material_results(form: dict[str, Any], now: str) -> list[dict[str, Any]]:
    submitted = form.get("materials") if isinstance(form.get("materials"), dict) else {}
    results = []
    for definition in _VENUE_MATERIAL_DEFINITIONS:
        applicable = bool(form.get(definition["trigger"], False)) if definition.get("trigger") else True
        if definition["material_id"] == "venue_application":
            content = {field: form.get(field) for field in definition["required_fields"]}
        else:
            content = submitted.get(definition["material_id"], {})
        if not applicable:
            status, missing = "not_applicable", []
        else:
            status, missing = _material_content_complete(content, definition["required_fields"])
        results.append(
            {
                "material_id": definition["material_id"],
                "label": definition["label"],
                "applicability": "required" if applicable else "not_applicable",
                "provenance": definition["provenance"],
                "content": content if isinstance(content, dict) else {},
                "status": status,
                "missing_fields": missing,
                "revision": 1,
                "updated_at": now,
            }
        )
    return results


def _venue_rule_results(form: dict[str, Any], reference_date: date) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    venue = str(form.get("venue", ""))
    results.append(
        {
            "rule_id": "venue-fixed-demo-plaza",
            "label": "场地为示例活动广场",
            "passed": venue == "demo_plaza",
            "detail": "已选择示例活动广场" if venue == "demo_plaza" else "当前演示只支持示例活动广场场景",
        }
    )

    raw_date = str(form.get("activity_date", ""))
    activity_date = None
    try:
        activity_date = date.fromisoformat(raw_date)
    except ValueError:
        pass
    lead_time_passed = bool(activity_date and activity_date >= _add_business_days(reference_date, 10))
    results.append(
        {
            "rule_id": "venue-lead-time-10-business-days",
            "label": "至少提前 10 个工作日",
            "passed": lead_time_passed,
            "detail": "已满足提前期" if lead_time_passed else "活动日期距离参考日期不足 10 个工作日或日期无效",
        }
    )

    open_day_passed = bool(activity_date and activity_date.weekday() in {4, 5, 6})
    results.append(
        {
            "rule_id": "venue-open-days",
            "label": "示例场景开放日为周五、周六或周日",
            "passed": open_day_passed,
            "detail": "日期属于开放日" if open_day_passed else "日期不在示例场景开放日范围内",
        }
    )

    start_time = str(form.get("start_time", ""))
    end_time = str(form.get("end_time", ""))
    order_passed = bool(start_time and end_time and start_time < end_time)
    results.append(
        {
            "rule_id": "venue-time-order",
            "label": "开始时间早于结束时间",
            "passed": order_passed,
            "detail": "时间段有效" if order_passed else "请填写开始时间和结束时间，且开始时间必须早于结束时间",
        }
    )

    if activity_date and activity_date.weekday() == 4:
        time_passed = start_time >= "17:30" and end_time <= "21:30" and order_passed
        window = "周五 17:30—21:30"
    else:
        time_passed = start_time >= "08:00" and end_time <= "21:30" and order_passed
        window = "周末 08:00—21:30"
    results.append(
        {
            "rule_id": "venue-time-window",
            "label": window,
            "passed": time_passed,
            "detail": "时段符合场景开放范围" if time_passed else f"请调整到 {window}",
        }
    )

    try:
        attendees = int(form.get("expected_attendees"))
    except (TypeError, ValueError):
        attendees = 0
    capacity_passed = attendees > 0
    results.append(
        {
            "rule_id": "venue-attendees-positive",
            "label": "预计人数为正整数",
            "passed": capacity_passed,
            "detail": "人数有效" if capacity_passed else "预计人数必须是大于 0 的整数",
        }
    )

    # Stable fixtures keep the scene repeatable without pretending to query a live calendar.
    selected_fixture = next(
        (
            fixture
            for fixture in _VENUE_VIRTUAL_SLOT_FIXTURES
            if fixture["date"] == raw_date
            and fixture["start_time"] == start_time
            and fixture["end_time"] == end_time
        ),
        None,
    )
    if selected_fixture and selected_fixture["availability"] == "conflict":
        slot = dict(selected_fixture)
    elif not lead_time_passed or not open_day_passed or not time_passed:
        slot = {
            "slot_id": f"slot-venue-rule-blocked-{raw_date or 'unknown'}-{start_time or 'unknown'}",
            "date": raw_date or None,
            "start_time": start_time or None,
            "end_time": end_time or None,
            "availability": "blocked_by_rule",
            "occupied_label": None,
            "alternatives": [],
        }
    else:
        slot = {
            "slot_id": f"slot-venue-available-{raw_date}-{start_time}-{end_time}",
            "date": raw_date,
            "start_time": start_time,
            "end_time": end_time,
            "availability": "available",
            "occupied_label": None,
            "alternatives": [],
        }
    slot["provenance"] = "project_rule"
    results.append(
        {
            "rule_id": "venue-virtual-slot",
            "label": "虚拟档期检查",
            "passed": slot["availability"] == "available",
            "detail": (
                "该时段可继续申请"
                if slot["availability"] == "available"
                else "该时段已有脱敏的场景占用，请选择替代时段"
                if slot["availability"] == "conflict"
                else "该时段被示例场景规则拦截，请调整日期或时间"
            ),
            **slot,
        }
    )
    return results


def _submissions_for(task_id: str) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            """SELECT id, task_id, submission_version, task_version, status, form_data,
                      rule_results, material_results, publication_id, source_snapshot_id,
                      review_note, created_at, updated_at
                 FROM simulation_task_submissions
                WHERE task_id = ?
                ORDER BY submission_version""",
            (task_id,),
        ).fetchall()
    result = []
    for row in rows:
        value = dict(row)
        for key in ("form_data", "rule_results", "material_results"):
            value[key] = json_loads(value[key], {} if key == "form_data" else [])
        result.append(value)
    return result


def _update_latest_submission(db, task_id: str, status: str, review_note: str = "") -> None:
    db.execute(
        """UPDATE simulation_task_submissions
              SET status = ?, review_note = CASE WHEN ? != '' THEN ? ELSE review_note END,
                  updated_at = ?
            WHERE task_id = ?
              AND submission_version = (
                    SELECT MAX(submission_version)
                      FROM simulation_task_submissions
                     WHERE task_id = ?
              )""",
        (status, review_note, review_note, now_iso(), task_id, task_id),
    )


def _task_view(row, events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    task = dict(row)
    for key in ("form_data", "missing_fields", "rule_results", "material_results", "impact_flags", "submission_snapshot"):
        task[key] = json_loads(task.get(key), None if key == "submission_snapshot" else {} if key == "form_data" else [])
    task["simulation"] = True
    task["display_disclaimer"] = "场景还原 · 演示数据 · 模拟办理 · 当前未接入学校真实业务系统"
    task["events"] = events or []
    task["submission_history"] = _submissions_for(task["id"])
    return task


_STUDENT_STATUS = {
    "draft": "draft",
    "collecting": "collecting",
    "precheck_failed": "precheck_failed",
    "ready_for_preview": "ready_for_preview",
    "awaiting_confirmation": "awaiting_confirmation",
    "submitted_simulated": "submitted",
    "under_simulated_review": "under_review",
    "correction_required": "needs_correction",
    "approved_simulated": "approved",
    "rejected_simulated": "rejected",
    "completed_simulated": "completed",
    "leave_closed_simulated": "closed",
    "needs_reconfirmation": "needs_reconfirmation",
    "policy_changed": "policy_changed",
    "cancelled": "cancelled",
}

_STUDENT_ROLE_LABELS = {
    "business_approver": "活动承办审核",
    "site_confirmation_recorder": "现场确认",
    "archive_operator": "材料归档",
    "business_knowledge_reviewer": "业务知识审核",
}

_STUDENT_RESULT_LABELS = {
    "submitted_simulated": "已提交",
    "under_simulated_review": "审核中",
    "correction_required": "需要补正",
    "approved_simulated": "已通过",
    "rejected_simulated": "未通过",
    "awaiting_offline_confirmation": "待现场确认",
    "archive_pending": "待材料归档",
    "completed_simulated": "已完成",
    "leave_closed_simulated": "已完成销假",
}

_STUDENT_NEXT_STEPS = {
    "under_simulated_review": "等待活动承办审核处理",
    "correction_required": "返回材料工作区，补充后重新提交",
    "rejected_simulated": "查看办理意见并结束当前申请",
    "approved_simulated": "按当前办理提示继续下一步",
    "awaiting_offline_confirmation": "按当前办理提示完成现场确认",
    "archive_pending": "等待材料归档",
    "completed_simulated": "办理流程已完成",
    "leave_closed_simulated": "办理流程已结束",
}

_VENUE_STUDENT_FIELDS = (
    {"key": "activity_name", "label": "活动名称", "type": "text", "required": True},
    {"key": "organization_type", "label": "组织类型", "type": "select", "options": ["student_organization", "class_group", "other"], "required": True},
    {"key": "organization_name", "label": "组织名称", "type": "text", "required": True},
    {"key": "venue", "label": "申请场地", "type": "select", "options": ["demo_plaza"], "required": True},
    {"key": "activity_date", "label": "活动日期", "type": "date", "required": True},
    {"key": "start_time", "label": "开始时间", "type": "time", "required": True},
    {"key": "end_time", "label": "结束时间", "type": "time", "required": True},
    {"key": "expected_attendees", "label": "预计人数", "type": "number", "required": True},
    {"key": "purpose", "label": "活动目的", "type": "textarea", "required": True},
    {"key": "has_sponsor", "label": "是否有赞助", "type": "checkbox", "required": True},
    {"key": "has_performance", "label": "是否有节目或演出", "type": "checkbox", "required": False},
    {"key": "has_promotion", "label": "是否需要宣传物料", "type": "checkbox", "required": False},
    {"key": "has_supplies", "label": "是否需要物资", "type": "checkbox", "required": False},
)


def _student_status(status: str) -> str:
    return _STUDENT_STATUS.get(status, "needs_review")


def _student_fields(task: dict[str, Any], scenario: dict[str, Any] | None) -> list[dict[str, Any]]:
    form = dict(task.get("form_data") or {})
    if scenario and scenario.get("slug") == "venue-application":
        form, _ = _canonical_venue_form(form)
        definitions = list(_VENUE_STUDENT_FIELDS)
    else:
        definitions = list((scenario or {}).get("fields") or [])
    missing = set(task.get("missing_fields") or [])
    fields = []
    for definition in definitions:
        key = definition["key"]
        fields.append(
            {
                "key": key,
                "label": definition.get("label", key),
                "type": definition.get("type", "text"),
                "options": definition.get("options", []),
                "value": form.get(key),
                "required": bool(definition.get("required")),
                "error": "请补充此项" if key in missing else None,
            }
        )
    return fields


def _student_materials(task: dict[str, Any]) -> list[dict[str, Any]]:
    materials = []
    status_map = {
        "not_started": "not_started",
        "editing": "editing",
        "ready": "saved",
        "saved": "saved",
        "needs_completion": "needs_completion",
        "submitted": "submitted",
        "returned": "returned",
        "revised": "revised",
        "approved": "approved",
        "not_applicable": "not_applicable",
        "已确认（演示）": "saved",
        "待确认": "needs_completion",
        "待选择": "needs_completion",
    }
    for item in task.get("material_results") or []:
        material_id = item.get("material_id") or item.get("rule_id")
        if not material_id:
            continue
        materials.append(
            {
                "id": material_id,
                "title": item.get("label") or item.get("name") or "办理材料",
                "required": item.get("applicability") in {"required", "conditional"} or bool(item.get("required")),
                "applicability": item.get("applicability", "required"),
                "type": "structured",
                "content": item.get("content") if isinstance(item.get("content"), dict) else {},
                "status": status_map.get(item.get("status"), "needs_completion"),
                "missing_fields": item.get("missing_fields", []),
                "correction_note": item.get("review_note"),
                "revision": item.get("revision", 1),
            }
        )
    return materials


def _student_rules(task: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "label": item.get("label", "办理条件"),
            "status": "passed" if item.get("passed") else "failed",
            "detail": item.get("detail", ""),
        }
        for item in task.get("rule_results") or []
    ]


def _student_review_nodes(task: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = []
    for event in task.get("events") or []:
        metadata = event.get("metadata") or {}
        target = metadata.get("to")
        if event.get("action") not in {"simulation.status_changed", "simulation.leave_closed"} and not target:
            continue
        nodes.append(
            {
                "role": _STUDENT_ROLE_LABELS.get(metadata.get("actor_role"), "事项处理人员"),
                "result": _STUDENT_RESULT_LABELS.get(target, "已记录") if target else "已完成销假",
                "note": metadata.get("reason") or "当前节点已记录",
                "next_step": _STUDENT_NEXT_STEPS.get(target, "请查看当前办理状态") if target else "请查看当前办理状态",
                "at": event.get("created_at"),
            }
        )
    return nodes


def _student_actions(status: str, materials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if status in {"needs_reconfirmation", "policy_changed"}:
        return [{"code": "reconfirm", "label": "重新核对办理依据", "enabled": True, "disabled_reason": None, "route": "reconfirmation", "required_fields": []}]
    if status in {"draft", "collecting", "precheck_failed", "correction_required", "needs_reconfirmation", "policy_changed"}:
        return [{"code": "fill_materials", "label": "填写或修改材料", "enabled": True, "disabled_reason": None, "route": "materials", "required_fields": []}]
    if status in {"ready_for_preview", "awaiting_confirmation"}:
        return [{"code": "open_preview", "label": "查看提交内容", "enabled": True, "disabled_reason": None, "route": "preview", "required_fields": []}]
    return [{"code": "view_status", "label": "查看办理状态", "enabled": True, "disabled_reason": None, "route": "status", "required_fields": []}]


def student_task_view(task: dict[str, Any]) -> dict[str, Any]:
    """Return only the student business projection, never the internal task row."""
    scenario = get_scenario(task.get("scenario_id")) if task.get("scenario_id") else None
    projection_task = dict(task)
    if not projection_task.get("material_results") and scenario and scenario.get("slug") == "venue-application":
        canonical_form, _ = _canonical_venue_form(dict(projection_task.get("form_data") or {}))
        projection_task["material_results"] = _venue_material_results(canonical_form, now_iso())
    materials = _student_materials(projection_task)
    status = _student_status(str(task.get("status", "draft")))
    scenario_title = (scenario or {}).get("name", "校园服务事项")
    if scenario_title.endswith("模拟"):
        scenario_title = scenario_title[:-2]
    return {
        "task_ref": {"task_id": task["id"], "route": f"/student/tasks/{task['id']}"},
        "scenario_title": scenario_title,
        "channel": task.get("channel", "standalone_web"),
        "status": status,
        "version": task.get("version", 1),
        "fields": _student_fields(task, scenario),
        "materials": materials,
        "rule_results": _student_rules(task),
        "review_nodes": _student_review_nodes(task),
        "next_actions": _student_actions(status, materials),
        "boundary_notice": "此页面使用脱敏、改写和虚拟化场景数据；不会提交到学校真实系统。",
    }


def student_preview_view(payload: dict[str, Any]) -> dict[str, Any]:
    task = payload.get("task") or {}
    preview = payload.get("preview") or {}
    view = student_task_view(task)
    return {
        "task": view,
        "preview": {
            "preview_version": preview.get("preview_version"),
            "submission_version": preview.get("submission_version"),
            "proposed_status": "submitted",
            "fields": view["fields"],
            "materials": view["materials"],
            "rules": view["rule_results"],
            "boundary_notice": view["boundary_notice"],
        },
    }


def _events_for(task_id: str) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            "SELECT id, actor_type, action, entity_type, entity_id, metadata, created_at FROM audit_events WHERE entity_id = ? ORDER BY created_at, id",
            (task_id,),
        ).fetchall()
    result = []
    for row in rows:
        value = dict(row)
        value["metadata"] = json_loads(value.get("metadata"), {})
        result.append(value)
    return result


def get_task(task_id: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM simulation_tasks WHERE id = ?", (task_id,)).fetchone()
    return _task_view(row, _events_for(task_id)) if row else None


def list_tasks(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute("SELECT * FROM simulation_tasks ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    return [_task_view(row) for row in rows]


def create_task(scenario_id: str, channel: str, actor_type: str, form_data: dict[str, Any]) -> dict[str, Any]:
    scenario = get_scenario(scenario_id)
    if not scenario:
        raise ValueError("scenario_not_found")
    task_id = new_id("task")
    now = now_iso()
    publication = get_current_publication()
    task = {
        "id": task_id,
        "scenario_id": scenario_id,
        "channel": channel,
        "actor_type": actor_type,
        "data_mode": "virtual_design",
        "status": "draft",
        "form_data": form_data,
        "missing_fields": [field["key"] for field in scenario["fields"] if field.get("required") and not form_data.get(field["key"])],
        "rule_results": [],
        "material_results": [],
        "publication_id": publication.get("id", ""),
        "source_snapshot_id": scenario["source_revision_id"],
        "submission_snapshot": None,
        "impact_flags": [],
        "idempotency_key": None,
        "version": 1,
        "created_at": now,
        "updated_at": now,
    }
    with connect() as db:
        db.execute(
            """INSERT INTO simulation_tasks
            (id, scenario_id, channel, actor_type, data_mode, status, form_data, missing_fields, rule_results, material_results,
             publication_id, source_snapshot_id, submission_snapshot, impact_flags, idempotency_key, version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id, scenario_id, channel, actor_type, task["data_mode"], task["status"], json_dumps(form_data), json_dumps(task["missing_fields"]),
                "[]", "[]", task["publication_id"], task["source_snapshot_id"], None, "[]", None, 1, now, now,
            ),
        )
    return get_task(task_id)


def precheck_task(
    task_id: str,
    form_data: dict[str, Any] | None = None,
    *,
    today: date | None = None,
    reference_date: date | None = None,
) -> dict[str, Any]:
    task = get_task(task_id)
    if not task:
        raise ValueError("task_not_found")
    scenario = get_scenario(task["scenario_id"])
    submitted_form = form_data or {}
    form = dict(task["form_data"])
    form.update(submitted_form)
    canonical_venue = scenario.get("slug") == "venue-application" and (
        not any(key in submitted_form for key in ("event_name", "date", "attendees", "organizer"))
        and any(key in form for key in ("activity_name", "activity_date", "expected_attendees"))
    )
    if scenario.get("slug") == "venue-application":
        form, legacy = _canonical_venue_form(form)
        canonical_venue = canonical_venue and not legacy
    if canonical_venue:
        required_venue_fields = (
            "activity_name",
            "organization_type",
            "organization_name",
            "venue",
            "activity_date",
            "start_time",
            "end_time",
            "expected_attendees",
            "purpose",
            "has_sponsor",
        )
        missing = [
            field
            for field in required_venue_fields
            if field not in form or (form[field] is not False and not _venue_value_present(form[field]))
        ]
        effective_reference_date = reference_date or today or datetime.now(timezone.utc).date()
        rules = _venue_rule_results(form, effective_reference_date)
        conditional_materials = []
        materials = _venue_material_results(form, now_iso())
    else:
        missing = [field["key"] for field in scenario["fields"] if field.get("required") and not form.get(field["key"])]
        rules = []
        conditional_materials = []
        for rule in scenario["rules"]:
            passed = True
            reason = "已满足演示规则"
            if rule["kind"] == "conditional_material":
                material, passed = _conditional_material_result(rule, form)
                conditional_materials.append(material)
                reason = material["detail"] if passed else material["status"]
            if rule["kind"] in {"positive_number"}:
                try:
                    passed = float(form.get(rule["field"], 0)) > 0
                except (TypeError, ValueError):
                    passed = False
                reason = "人数需要是正数" if not passed else reason
            if rule["kind"] == "checkbox_required":
                passed = bool(form.get(rule["field"]))
                reason = "请先确认已阅读资格条件" if not passed else reason
            if rule["kind"] == "lead_time" and form.get("date"):
                try:
                    target = datetime.fromisoformat(str(form["date"]))
                    effective_reference_date = reference_date or today or datetime.now(timezone.utc).date()
                    passed = target.date() >= _add_business_days(effective_reference_date, 3)
                    reason = "活动日期距离当前不足 3 个工作日" if not passed else reason
                except ValueError:
                    passed = False
                    reason = "日期格式无法核验"
            if rule["kind"] == "opening_hours" and form.get("start_time") and form.get("end_time"):
                is_weekday = True
                if form.get("date"):
                    try:
                        is_weekday = datetime.fromisoformat(str(form["date"])).weekday() < 5
                    except ValueError:
                        is_weekday = False
                passed = is_weekday and str(form["start_time"]) >= "08:00" and str(form["end_time"]) <= "22:00" and str(form["start_time"]) < str(form["end_time"])
                reason = "演示开放时段为工作日 08:00—22:00" if not passed else reason
            rules.append({"rule_id": rule["id"], "label": rule["label"], "passed": passed, "detail": reason})
    if scenario.get("slug") == "work-study":
        job = get_job(str(form.get("job_id")), "demo_student") if form.get("job_id") else None
        job_passed = bool(job and job.get("status") == "published")
        rules.append(
            {
                "rule_id": "workstudy-job-published",
                "label": "岗位必须处于已发布状态",
                "passed": job_passed,
                "detail": "已匹配学生可见岗位" if job_passed else "岗位不存在、未发布或已关闭",
            }
        )
    if not canonical_venue:
        materials_confirmed = bool(form.get("materials_confirmed"))
        materials = [
            {
                "rule_id": f"material-{index}",
                "name": name,
                "status": "已确认（演示）" if materials_confirmed else "待确认",
                "required": True,
                "detail": "仅确认已准备演示材料，不上传或保存真实个人材料",
            }
            for index, name in enumerate(scenario["materials"], start=1)
        ]
        materials.extend(conditional_materials)
        all_materials_passed = all(material["status"] == "已确认（演示）" for material in materials)
    else:
        all_materials_passed = all(material["status"] in {"ready", "not_applicable"} for material in materials)
    all_passed = not missing and all(rule["passed"] for rule in rules) and all_materials_passed
    status = "ready_for_preview" if all_passed else "precheck_failed"
    now = now_iso()
    with connect() as db:
        updated = db.execute(
            """UPDATE simulation_tasks SET form_data = ?, missing_fields = ?, rule_results = ?, material_results = ?, status = ?, updated_at = ?, version = version + 1
            WHERE id = ? AND version = ?""",
            (json_dumps(form), json_dumps(missing), json_dumps(rules), json_dumps(materials), status, now, task_id, task["version"]),
        ).rowcount
        if updated != 1:
            raise ValueError("task_concurrent_update")
    return get_task(task_id)


def save_materials(
    task_id: str,
    materials: dict[str, Any],
    expected_version: int,
    form_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist structured material drafts without pretending that they are uploaded files."""
    task = get_task(task_id)
    if not task:
        raise ValueError("task_not_found")
    if task["status"] in {"submitted_simulated", "under_simulated_review", "approved_simulated", "completed_simulated", "cancelled"}:
        raise ValueError("material_update_not_allowed")
    if task["version"] != expected_version:
        raise ValueError("task_concurrent_update")

    updated_form = dict(task["form_data"])
    if isinstance(form_data, dict):
        updated_form.update(form_data)
    existing = updated_form.get("materials") if isinstance(updated_form.get("materials"), dict) else {}
    updated_form["materials"] = {**existing, **materials}
    now = now_iso()
    scenario = get_scenario(task["scenario_id"])
    if scenario and scenario.get("slug") == "venue-application":
        material_results = _venue_material_results(updated_form, now)
    else:
        material_results = task["material_results"]

    with connect() as db:
        updated = db.execute(
            """UPDATE simulation_tasks
            SET form_data = ?, material_results = ?, status = 'collecting', updated_at = ?, version = version + 1
            WHERE id = ? AND version = ?""",
            (json_dumps(updated_form), json_dumps(material_results), now, task_id, expected_version),
        ).rowcount
        if updated != 1:
            raise ValueError("task_concurrent_update")
        db.execute(
            "INSERT INTO audit_events VALUES (?, 'demo_student', 'simulation.materials_saved', 'simulation_task', ?, ?, ?)",
            (new_id("audit"), task_id, json_dumps({"material_ids": sorted(materials), "simulation": True}), now),
        )
    return get_task(task_id)


def preview_task(task_id: str) -> dict[str, Any]:
    task = get_task(task_id)
    if not task:
        raise ValueError("task_not_found")
    if task["status"] not in {"ready_for_preview", "awaiting_confirmation"}:
        raise ValueError("task_not_ready_for_preview")
    scenario = get_scenario(task["scenario_id"])
    return {
        "task": task,
        "preview": {
            "preview_version": task["version"],
            "submission_version": (task["submission_history"][-1]["submission_version"] if task["submission_history"] else 0) + 1,
            "proposed_status": "submitted_simulated",
            "fields": task["form_data"],
            "rules": task["rule_results"],
            "materials": task["material_results"],
            "source_snapshot_id": task["source_snapshot_id"],
            "publication_id": task["publication_id"],
            "scenario": scenario["name"] if scenario else "演示场景",
            "simulation": True,
            "disclaimer": "只写入庆事通演示数据库，不会访问学校真实系统。",
        },
    }


def confirm_task(
    task_id: str,
    idempotency_key: str,
    confirmed: bool,
    *,
    expected_version: int | None = None,
    preview_version: int | None = None,
) -> dict[str, Any]:
    task = get_task(task_id)
    if not task:
        raise ValueError("task_not_found")
    if task.get("idempotency_key") == idempotency_key:
        return task
    if not confirmed:
        return task
    if preview_version is not None and task["version"] != preview_version:
        raise ValueError("preview_expired")
    if expected_version is not None and task["version"] != expected_version:
        raise ValueError("task_concurrent_update")
    if task["status"] != "ready_for_preview":
        raise ValueError("confirmation_not_allowed")
    now = now_iso()
    snapshot = {
        "form_data": task["form_data"],
        "material_results": task["material_results"],
        "publication_id": task["publication_id"],
        "source_snapshot_id": task["source_snapshot_id"],
        "preview_version": task["version"],
        "submission_version": (task["submission_history"][-1]["submission_version"] if task["submission_history"] else 0) + 1,
        "supersedes_submission_version": task["submission_history"][-1]["submission_version"] if task["submission_history"] else None,
        "rule_results": task["rule_results"],
        "confirmed_at": now,
        "simulation": True,
    }
    with connect() as db:
        submission_version = snapshot["submission_version"]
        updated = db.execute(
            """UPDATE simulation_tasks SET status = 'submitted_simulated', submission_snapshot = ?, idempotency_key = ?, updated_at = ?, version = version + 1
            WHERE id = ? AND status = 'ready_for_preview' AND idempotency_key IS NULL""",
            (json_dumps(snapshot), idempotency_key, now, task_id),
        ).rowcount
        if updated != 1:
            current = get_task(task_id)
            if current and current.get("idempotency_key") == idempotency_key:
                return current
            raise ValueError("confirmation_not_allowed")
        db.execute(
            """INSERT INTO simulation_task_submissions
                (id, task_id, submission_version, task_version, status, form_data, rule_results,
                 material_results, publication_id, source_snapshot_id, review_note, created_at, updated_at)
             VALUES (?, ?, ?, ?, 'submitted_simulated', ?, ?, ?, ?, ?, '', ?, ?)""",
            (
                new_id("submission"),
                task_id,
                submission_version,
                task["version"],
                json_dumps(task["form_data"]),
                json_dumps(task["rule_results"]),
                json_dumps(task["material_results"]),
                task["publication_id"],
                task["source_snapshot_id"],
                now,
                now,
            ),
        )
        db.execute(
            "INSERT INTO audit_events VALUES (?, 'demo_student', 'simulation.confirmed', 'simulation_task', ?, ?, ?)" ,
            (new_id("audit"), task_id, json_dumps({"simulation": True, "idempotency_key": idempotency_key, "submission_version": submission_version}), now),
        )
    return get_task(task_id)


def advance_task(
    task_id: str,
    target_status: str,
    actor_type: str = "demo_reviewer",
    *,
    reason: str = "",
    expected_version: int | None = None,
    idempotency_key: str | None = None,
    actor_role: str | None = None,
    governance_session_id: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    task = get_task(task_id)
    if not task:
        raise ValueError("task_not_found")
    if idempotency_key:
        with connect() as db:
            existing = db.execute(
                "SELECT result FROM idempotency_records WHERE operation = ? AND idempotency_key = ?",
                (f"simulation-review:{task_id}", idempotency_key),
            ).fetchone()
        if existing:
            return json_loads(existing["result"], task)
    if expected_version is not None and task["version"] != expected_version:
        raise ValueError("task_concurrent_update")
    allowed = {
        "submitted_simulated": {"under_simulated_review"},
        "under_simulated_review": {"correction_required", "completed_simulated", "rejected_simulated"},
        "correction_required": {"under_simulated_review"},
    }
    if task["scenario_id"] == "scenario-venue-v1":
        allowed["under_simulated_review"] = {
            "correction_required",
            "rejected_simulated",
            "awaiting_offline_confirmation",
        }
        allowed["awaiting_offline_confirmation"] = {"archive_pending"}
        allowed["archive_pending"] = {"completed_simulated"}
    if task["scenario_id"] == "scenario-leave-v1":
        allowed["under_simulated_review"] = {"correction_required", "approved_simulated", "rejected_simulated"}
        allowed["approved_simulated"] = {"completed_simulated"}
    if target_status not in allowed.get(task["status"], set()):
        raise ValueError("invalid_transition")
    role_by_status = {
        "under_simulated_review": "business_approver",
        "correction_required": "business_approver",
        "rejected_simulated": "business_approver",
        "approved_simulated": "business_approver",
        "awaiting_offline_confirmation": "business_approver",
        "archive_pending": "site_confirmation_recorder",
        "completed_simulated": "archive_operator",
    }
    required_role = role_by_status.get(target_status, "business_approver")
    if actor_role is not None and actor_role != required_role:
        raise ValueError("governance_role_forbidden")
    actor_role = actor_role or required_role
    now = now_iso()
    event_reason = reason or f"状态推进至 {target_status}"
    with connect() as db:
        next_idempotency_key = None if target_status == "correction_required" else task["idempotency_key"]
        updated = db.execute(
            "UPDATE simulation_tasks SET status = ?, idempotency_key = ?, updated_at = ?, version = version + 1 WHERE id = ? AND version = ?",
            (target_status, next_idempotency_key, now, task_id, task["version"]),
        ).rowcount
        if updated != 1:
            raise ValueError("task_concurrent_update")
        _update_latest_submission(db, task_id, target_status, event_reason)
        db.execute(
            "INSERT INTO audit_events VALUES (?, ?, 'simulation.status_changed', 'simulation_task', ?, ?, ?)",
            (
                new_id("audit"),
                actor_type,
                task_id,
                json_dumps(
                    {
                        "from": task["status"],
                        "to": target_status,
                        "actor_role": actor_role,
                        "reason": event_reason,
                        "governance_session_id": governance_session_id,
                        "request_id": request_id,
                        "submission_version": (task.get("submission_snapshot") or {}).get("preview_version"),
                        "submission_record_version": task["submission_history"][-1]["submission_version"] if task["submission_history"] else None,
                        "simulation": True,
                    }
                ),
                now,
            ),
        )
    result = get_task(task_id)
    if idempotency_key and result:
        with connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO idempotency_records (operation, idempotency_key, result, created_at) VALUES (?, ?, ?, ?)",
                (f"simulation-review:{task_id}", idempotency_key, json_dumps(result), now),
            )
    return result


def close_leave_task(task_id: str, return_date: str, confirmed: bool = True) -> dict[str, Any]:
    task = get_task(task_id)
    if not task:
        raise ValueError("task_not_found")
    if task["scenario_id"] != "scenario-leave-v1":
        raise ValueError("leave_close_not_allowed")
    if task["status"] not in {"approved_simulated", "completed_simulated"}:
        raise ValueError("leave_close_not_allowed")
    if not confirmed:
        return task
    try:
        datetime.fromisoformat(return_date)
    except ValueError as error:
        raise ValueError("invalid_return_date") from error
    now = now_iso()
    snapshot = dict(task.get("submission_snapshot") or {})
    snapshot["closure"] = {"return_date": return_date, "confirmed_at": now, "simulation": True}
    with connect() as db:
        updated = db.execute(
            "UPDATE simulation_tasks SET status = 'leave_closed_simulated', submission_snapshot = ?, updated_at = ?, version = version + 1 WHERE id = ? AND status IN ('approved_simulated', 'completed_simulated') AND version = ?",
            (json_dumps(snapshot), now, task_id, task["version"]),
        ).rowcount
        if updated != 1:
            raise ValueError("task_concurrent_update")
        db.execute(
            "INSERT INTO audit_events VALUES (?, 'demo_reviewer', 'simulation.leave_closed', 'simulation_task', ?, ?, ?)",
            (new_id("audit"), task_id, json_dumps({"return_date": return_date, "simulation": True}), now),
        )
    return get_task(task_id)


def mark_tasks_for_source_change(source_revision_id: str, impact_event_id: str, *, connection=None) -> list[str]:
    def _mark(db) -> list[str]:
        rows = db.execute(
            "SELECT id, status, impact_flags FROM simulation_tasks WHERE source_snapshot_id = ? AND status NOT IN ('completed_simulated', 'cancelled')",
            (source_revision_id,),
        ).fetchall()
        affected = []
        now = now_iso()
        for row in rows:
            flags = json_loads(row["impact_flags"], [])
            if any(flag.get("impact_event_id") == impact_event_id for flag in flags if isinstance(flag, dict)):
                continue
            flags.append({"impact_event_id": impact_event_id, "reason": "依赖的来源版本发生变化"})
            new_status = "policy_changed" if row["status"] in {"submitted_simulated", "under_simulated_review"} else "needs_reconfirmation"
            db.execute(
                "UPDATE simulation_tasks SET status = ?, impact_flags = ?, updated_at = ?, version = version + 1 WHERE id = ?",
                (new_status, json_dumps(flags), now, row["id"]),
            )
            db.execute(
                "INSERT INTO audit_events VALUES (?, 'demo_reviewer', 'simulation.source_impact', 'simulation_task', ?, ?, ?)",
                (new_id("audit"), row["id"], json_dumps({"impact_event_id": impact_event_id, "status": new_status}), now),
            )
            affected.append(row["id"])
        return affected

    if connection is not None:
        return _mark(connection)
    with connect() as db:
        return _mark(db)


def _basis_view(row) -> dict[str, Any]:
    if not row:
        return {"source_revision_id": None, "title": None, "publisher": None, "freshness_state": None}
    return {
        "source_revision_id": row["id"],
        "title": row["title"],
        "publisher": row["publisher"],
        "published_at": row["published_at"],
        "effective_from": row["effective_from"],
        "effective_to": row["effective_to"],
        "freshness_state": row["freshness_state"],
    }


def get_reconfirmation(task_id: str, impact_event_id: str) -> dict[str, Any]:
    task = get_task(task_id)
    if not task:
        raise ValueError("task_not_found")
    if task["status"] not in {"needs_reconfirmation", "policy_changed"}:
        raise ValueError("reconfirmation_not_allowed")
    if not any(
        isinstance(flag, dict) and flag.get("impact_event_id") == impact_event_id
        for flag in task["impact_flags"]
    ):
        raise ValueError("reconfirmation_not_allowed")
    with connect() as db:
        impact = db.execute("SELECT * FROM impact_events WHERE id = ?", (impact_event_id,)).fetchone()
        if not impact:
            raise ValueError("impact_event_not_found")
        affected_tasks = json_loads(impact["affected_tasks"], [])
        if affected_tasks and task_id not in affected_tasks:
            raise ValueError("reconfirmation_not_allowed")
        old_basis = db.execute("SELECT * FROM source_revisions WHERE id = ?", (task["source_snapshot_id"],)).fetchone()
        new_basis = db.execute("SELECT * FROM source_revisions WHERE id = ?", (impact["source_revision_id"],)).fetchone()
        current_publication = db.execute(
            """SELECT p.id
                 FROM publications p
                 JOIN publication_bindings pb ON pb.publication_id = p.id
                WHERE p.status IN ('active', 'published')
                  AND pb.revision_id = ?
                ORDER BY p.published_at DESC, p.created_at DESC
                LIMIT 1""",
            (impact["source_revision_id"],),
        ).fetchone()
    if not new_basis or not current_publication or impact["status"] != "published":
        raise ValueError("reconfirmation_not_allowed")
    return {
        "task_id": task_id,
        "status": task["status"],
        "impact_event_id": impact_event_id,
        "change_categories": json_loads(impact["change_categories"], []),
        "severity": impact["severity"],
        "old_basis": _basis_view(old_basis),
        "new_basis": {**_basis_view(new_basis), "publication_id": current_publication["id"]},
        "saved_fields": task["form_data"],
        "saved_materials": task["material_results"],
        "submission_history": task["submission_history"],
        "action": {
            "code": "reconfirm",
            "label": "重新核对并确认当前办理依据",
            "required": True,
        },
    }


def _pending_impact_event_id(task: dict[str, Any]) -> str:
    for flag in reversed(task.get("impact_flags") or []):
        if isinstance(flag, dict) and flag.get("impact_event_id") and not flag.get("acknowledged_at"):
            return str(flag["impact_event_id"])
    raise ValueError("reconfirmation_not_allowed")


def get_pending_reconfirmation(task_id: str) -> dict[str, Any]:
    task = get_task(task_id)
    if not task:
        raise ValueError("task_not_found")
    return get_reconfirmation(task_id, _pending_impact_event_id(task))


def _student_basis_view(basis: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": basis.get("title"),
        "publisher": basis.get("publisher"),
        "published_at": basis.get("published_at"),
        "effective_from": basis.get("effective_from"),
        "effective_to": basis.get("effective_to"),
        "freshness_state": basis.get("freshness_state"),
    }


def student_reconfirmation_view(payload: dict[str, Any]) -> dict[str, Any]:
    task = get_task(payload["task_id"])
    if not task:
        raise ValueError("task_not_found")
    task_view = student_task_view(task)
    category_labels = {
        "procedure_change": "办理步骤发生变化",
        "freshness_change": "依据时效发生变化",
        "material_change": "办理材料发生变化",
        "condition_change": "办理条件发生变化",
    }
    change_summary = [category_labels.get(item, "办理依据需要重新核对") for item in payload.get("change_categories", [])]
    if not change_summary:
        change_summary = ["办理依据需要重新核对"]
    return {
        "status": _student_status(payload["status"]),
        "version": task_view["version"],
        "change_summary": change_summary,
        "severity": "需要优先核对" if payload.get("severity") == "high" else "需要核对",
        "old_basis": _student_basis_view(payload["old_basis"]),
        "new_basis": _student_basis_view(payload["new_basis"]),
        "saved_fields": task_view["fields"],
        "saved_materials": task_view["materials"],
        "action": {
            "code": "reconfirm",
            "label": "重新核对并确认当前办理依据",
            "required": True,
        },
        "boundary_notice": task_view["boundary_notice"],
    }


def get_student_reconfirmation(task_id: str) -> dict[str, Any]:
    return student_reconfirmation_view(get_pending_reconfirmation(task_id))


def _reconfirmation_operation(task_id: str) -> str:
    return f"simulation-reconfirmation:{task_id}"


def _cached_reconfirmation(task_id: str, idempotency_key: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute(
            "SELECT result FROM idempotency_records WHERE operation = ? AND idempotency_key = ?",
            (_reconfirmation_operation(task_id), idempotency_key),
        ).fetchone()
    return json_loads(row["result"], None) if row else None


def _save_reconfirmation_result(task_id: str, idempotency_key: str, result: dict[str, Any]) -> None:
    with connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO idempotency_records (operation, idempotency_key, result, created_at) VALUES (?, ?, ?, ?)",
            (_reconfirmation_operation(task_id), idempotency_key, json_dumps(result), now_iso()),
        )


def reconfirm_pending_task(task_id: str, confirmed: bool, expected_version: int, idempotency_key: str) -> dict[str, Any]:
    cached = _cached_reconfirmation(task_id, idempotency_key)
    if cached is not None:
        return cached
    pending = get_pending_reconfirmation(task_id)
    return reconfirm_task(task_id, pending["impact_event_id"], confirmed, expected_version, idempotency_key)


def reconfirm_task(task_id: str, impact_event_id: str, confirmed: bool, expected_version: int, idempotency_key: str) -> dict[str, Any]:
    cached = _cached_reconfirmation(task_id, idempotency_key)
    if cached is not None:
        return cached
    view = get_reconfirmation(task_id, impact_event_id)
    task = get_task(task_id)
    if task["version"] != expected_version:
        raise ValueError("task_concurrent_update")
    if not confirmed:
        return task
    now = now_iso()
    new_revision_id = view["new_basis"]["source_revision_id"]
    new_publication_id = view["new_basis"]["publication_id"]
    flags = []
    for flag in task["impact_flags"]:
        if isinstance(flag, dict) and flag.get("impact_event_id") == impact_event_id:
            flags.append({**flag, "acknowledged_at": now})
        else:
            flags.append(flag)
    with connect() as db:
        updated = db.execute(
            """UPDATE simulation_tasks
                  SET status = 'collecting', publication_id = ?, source_snapshot_id = ?,
                      idempotency_key = NULL, impact_flags = ?, updated_at = ?, version = version + 1
                WHERE id = ? AND version = ? AND status IN ('needs_reconfirmation', 'policy_changed')""",
            (new_publication_id, new_revision_id, json_dumps(flags), now, task_id, expected_version),
        ).rowcount
        if updated != 1:
            raise ValueError("task_concurrent_update")
        db.execute(
            "INSERT INTO audit_events VALUES (?, 'demo_student', 'simulation.reconfirmed', 'simulation_task', ?, ?, ?)",
            (new_id("audit"), task_id, json_dumps({"impact_event_id": impact_event_id, "old_source_revision_id": task["source_snapshot_id"], "new_source_revision_id": new_revision_id, "publication_id": new_publication_id}), now),
        )
    result = get_task(task_id)
    if result is None:
        raise ValueError("task_not_found")
    _save_reconfirmation_result(task_id, idempotency_key, result)
    return result
