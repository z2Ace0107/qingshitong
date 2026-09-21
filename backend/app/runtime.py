from __future__ import annotations

import uuid
import re
from datetime import datetime, timezone
from threading import Event, RLock, Thread
import time
from typing import Any

from .agent import AgentConfig, AgentModelError, run_agent_loop
from .db import connect, json_dumps, json_loads
from .knowledge import get_current_publication, get_item_by_slug, get_scenario, search, student_business_text, validate_claim_evidence
from .publication import get_publication
from .redaction import redact_text, redact_value
from .tooling import registered_tool_ids


class RunCancelled(RuntimeError):
    """The active stream was cancelled before its result was committed."""


class _StreamControl:
    def __init__(self) -> None:
        self.cancel_event = Event()
        self.cancel_result: dict[str, Any] | None = None


_STREAM_CONTROLS: dict[str, _StreamControl] = {}
_STREAM_LOCK = RLock()
STREAM_SCHEMA_VERSION = "qst.stream.v1"
STREAM_HEARTBEAT_SECONDS = 15.0
STREAM_CONFIG_VERSION = "qst.stream-config.v1"
STREAM_MAX_EVENT_BYTES = 64 * 1024
STREAM_MAX_FINAL_RESPONSE_BYTES = 128 * 1024


class StreamPayloadTooLarge(RuntimeError):
    """A student-facing stream frame exceeded the R0 transport contract."""


def _stream_new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _raise_if_cancelled(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise RunCancelled("cancelled")


def start_stream_run(run_id: str, *, session_id: str, channel: str, message: str) -> None:
    """Create a recoverable shell before the streaming response starts."""

    created_at = now_iso()
    with connect() as db:
        db.execute(
            """INSERT INTO runs (id, feedback_ref, session_id, channel, input_text, status, intent, context_manifest, steps, response, created_at, completed_at, error_code)
            VALUES (?, ?, ?, ?, ?, 'running', '{}', ?, '[]', '{}', ?, NULL, NULL)
            ON CONFLICT(id) DO NOTHING""",
            (
                run_id,
                None,
                session_id,
                channel,
                redact_text(message),
                json_dumps({"stream_config": stream_config()}),
                created_at,
            ),
        )
    with _STREAM_LOCK:
        _STREAM_CONTROLS[run_id] = _StreamControl()


def request_stream_cancel(run_id: str, idempotency_key: str, *, session_id: str) -> dict[str, Any]:
    """Request cancellation once and return the same result for repeats."""

    def persist_request() -> dict[str, Any]:
        with connect() as db:
            row = db.execute("SELECT status, session_id FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise ValueError("run_not_found")
            if row["session_id"] != session_id:
                raise ValueError("run_session_forbidden")
            if row["status"] == "running":
                db.execute(
                    "UPDATE runs SET status = 'cancel_requested' WHERE id = ? AND status = 'running'",
                    (run_id,),
                )
            current = db.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        return {"status": current["status"]}

    with _STREAM_LOCK:
        control = _STREAM_CONTROLS.get(run_id)
        if control is not None:
            if control.cancel_result is None:
                persisted = persist_request()
                if persisted["status"] in {"running", "cancel_requested"}:
                    control.cancel_event.set()
                    control.cancel_result = {
                        "run_id": run_id,
                        "status": "cancel_requested",
                        "terminal": False,
                        "idempotency_key": idempotency_key,
                    }
                else:
                    control.cancel_result = {
                        "run_id": run_id,
                        "status": persisted["status"],
                        "terminal": True,
                        "idempotency_key": idempotency_key,
                    }
            return dict(control.cancel_result)

    persisted = persist_request()
    return {
        "run_id": run_id,
        "status": persisted["status"],
        "terminal": persisted["status"] not in {"running", "cancel_requested"},
        "idempotency_key": idempotency_key,
    }


def stream_config() -> dict[str, Any]:
    return {
        "version": STREAM_CONFIG_VERSION,
        "heartbeat_seconds": STREAM_HEARTBEAT_SECONDS,
        "max_event_bytes": STREAM_MAX_EVENT_BYTES,
        "max_final_response_bytes": STREAM_MAX_FINAL_RESPONSE_BYTES,
    }


def _finish_stream_run(
    run_id: str,
    *,
    status: str,
    error_code: str,
    response: dict[str, Any],
    detail: str,
    overwrite_terminal: bool = False,
) -> None:
    completed_at = now_iso()
    step = {
        "step_id": f"step-{run_id}-stream-terminal",
        "run_id": run_id,
        "name": "流式查询终态",
        "status": status,
        "detail": detail,
        "created_at": completed_at,
    }
    with connect() as db:
        allowed_statuses = "('running', 'cancel_requested')"
        if overwrite_terminal:
            allowed_statuses = "('running', 'cancel_requested', 'completed', 'degraded', 'needs_review', 'refused', 'waiting_user')"
        db.execute(
            f"UPDATE runs SET status = ?, response = ?, steps = ?, completed_at = ?, error_code = ? WHERE id = ? AND status IN {allowed_statuses}",
            (status, json_dumps(response), json_dumps([step]), completed_at, error_code, run_id),
        )
        db.execute("DELETE FROM run_steps WHERE run_id = ?", (run_id,))
        db.execute(
            "INSERT INTO run_steps (step_id, run_id, step_index, name, status, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (step["step_id"], run_id, 0, step["name"], step["status"], step["detail"], completed_at),
        )
        db.execute(
            "INSERT INTO audit_events VALUES (?, 'demo_student', ?, 'run', ?, ?, ?)",
            (_stream_new_id("audit"), f"runtime.run.{status}", run_id, json_dumps({"stream": True}), completed_at),
        )


def _stream_event(run_id: str, sequence: int, event_type: str, payload: dict[str, Any], *, terminal: bool = False) -> dict[str, Any]:
    return {
        "schema_version": STREAM_SCHEMA_VERSION,
        "event_id": _stream_new_id("event"),
        "run_id": run_id,
        "sequence": sequence,
        "type": event_type,
        "terminal": terminal,
        "payload": payload,
    }


def _sse_event(event: dict[str, Any]) -> str:
    rendered = "event: {0}\nid: {1}\ndata: {2}\n\n".format(
        event["type"],
        event["event_id"],
        json_dumps(event),
    )
    if len(rendered.encode("utf-8")) > STREAM_MAX_EVENT_BYTES:
        raise StreamPayloadTooLarge("stream_event_too_large")
    return rendered


def stream_query(
    message: str,
    session_id: str | None,
    channel: str,
    task_id: str | None,
    *,
    agent_config: AgentConfig,
) -> Any:
    """Yield business progress and one validated terminal projection."""

    run_id = _stream_new_id("run")
    session_id = session_id or _stream_new_id("session")
    start_stream_run(run_id, session_id=session_id, channel=channel, message=message)
    with _STREAM_LOCK:
        control = _STREAM_CONTROLS[run_id]

    worker_thread: Thread | None = None
    sequence = 1
    try:
        yield _sse_event(_stream_event(run_id, sequence, "run.started", {"message": "正在准备查询"}))
        sequence += 1
        yield _sse_event(_stream_event(run_id, sequence, "progress", {"message": "正在查找相关事项"}))
        sequence += 1
    except GeneratorExit:
        control.cancel_event.set()
        _finish_stream_run(
            run_id,
            status="cancelled",
            error_code="client_disconnected",
            response={"kind": "cancelled", "status": "cancelled", "summary": "本次查询已结束。", "next_actions": ["重新提交"]},
            detail="客户端连接中断，服务端未继续提交学生回答",
        )
        with _STREAM_LOCK:
            _STREAM_CONTROLS.pop(run_id, None)
        raise

    result_box: dict[str, Any] = {}

    def worker() -> None:
        try:
            result_box["result"] = run_query(
                message,
                session_id,
                channel,
                task_id,
                "c",
                agent_config=agent_config,
                run_id=run_id,
                cancel_event=control.cancel_event,
                streaming=True,
            )
        except RunCancelled as error:
            result_box["cancelled"] = error
        except Exception as error:  # noqa: BLE001 - the public stream must fail closed
            result_box["error"] = error

    worker_thread = Thread(target=worker, name=f"qst-stream-{run_id}", daemon=True)
    worker_thread.start()
    progress_sent = False
    terminal_emitted = False
    last_heartbeat = time.monotonic()
    try:
        while worker_thread.is_alive():
            if control.cancel_event.is_set() and not progress_sent:
                yield _sse_event(_stream_event(run_id, sequence, "progress", {"message": "正在结束本次查询"}))
                sequence += 1
                progress_sent = True
            if time.monotonic() - last_heartbeat >= STREAM_HEARTBEAT_SECONDS:
                yield ": keep-alive\n\n"
                last_heartbeat = time.monotonic()
            time.sleep(0.02)
        worker_thread.join()

        current_run = get_run(run_id)
        cancellation_pending = control.cancel_event.is_set() and (
            current_run is None or current_run.get("status") in {"running", "cancel_requested"}
        )
        if "cancelled" in result_box or cancellation_pending:
            response = {"kind": "cancelled", "status": "cancelled", "summary": "本次查询已结束。", "next_actions": ["重新提交"]}
            _finish_stream_run(run_id, status="cancelled", error_code="cancelled", response=response, detail="用户请求取消，未提交学生回答")
            terminal_emitted = True
            yield _sse_event(_stream_event(run_id, sequence, "run.cancelled", {"message": "本次查询已结束", "next_actions": ["重新提交"]}, terminal=True))
            return

        if "error" in result_box:
            response = {"kind": "failed", "status": "failed", "summary": "暂时无法完成查询。", "next_actions": ["稍后重试"]}
            _finish_stream_run(run_id, status="failed", error_code="stream_internal_error", response=response, detail="流式查询在服务端异常结束")
            terminal_emitted = True
            yield _sse_event(_stream_event(run_id, sequence, "run.failed", {"message": "暂时无法完成查询", "recovery_action": "retry_later"}, terminal=True))
            return

        result = result_box["result"]
        student_response = student_response_view(result["response"])
        if len(json_dumps(student_response).encode("utf-8")) > STREAM_MAX_FINAL_RESPONSE_BYTES:
            response = {"kind": "failed", "status": "failed", "summary": "暂时无法完成查询。", "next_actions": ["稍后重试"]}
            _finish_stream_run(
                run_id,
                status="failed",
                error_code="final_response_too_large",
                response=response,
                detail="最终学生回答超过流式缓冲上限，服务端拒绝截断后发送",
                overwrite_terminal=True,
            )
            terminal_emitted = True
            yield _sse_event(_stream_event(run_id, sequence, "run.failed", {"message": "暂时无法完成查询", "recovery_action": "retry_later"}, terminal=True))
            return
        response_kind = result["response"].get("kind")
        agent_result = result["response"].get("agent") or {}
        if response_kind == "refused":
            if result["response"].get("review_required"):
                event_type = "run.needs_review"
                status = "needs_review"
            else:
                event_type = "run.refused"
                status = "refused"
        elif result["response"].get("degradation") or agent_result.get("fallback"):
            event_type = "run.degraded"
            status = "degraded"
        else:
            event_type = "run.completed"
            status = "completed"
        terminal_emitted = True
        yield _sse_event(_stream_event(run_id, sequence, event_type, {"response": student_response}, terminal=True))
    finally:
        if not terminal_emitted:
            control.cancel_event.set()
            if worker_thread is not None:
                worker_thread.join(timeout=1.0)
            current_run = get_run(run_id)
            if current_run is not None and current_run.get("status") in {"running", "cancel_requested"}:
                _finish_stream_run(
                    run_id,
                    status="cancelled",
                    error_code="client_disconnected",
                    response={"kind": "cancelled", "status": "cancelled", "summary": "本次查询已结束。", "next_actions": ["重新提交"]},
                    detail="客户端连接中断，服务端未继续提交学生回答",
                )
        with _STREAM_LOCK:
            _STREAM_CONTROLS.pop(run_id, None)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def recover_incomplete_runs() -> int:
    """Mark unfinished model runs as interrupted without replaying any action."""
    recovered_at = now_iso()
    with connect() as db:
        result = db.execute(
            """UPDATE runs
            SET status = 'interrupted', completed_at = ?, error_code = 'process_restart'
            WHERE status = 'running' AND completed_at IS NULL""",
            (recovered_at,),
        )
    return result.rowcount


def get_run(run_id: str) -> dict[str, Any] | None:
    with connect() as db:
        run = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not run:
            return None
        steps = db.execute(
            "SELECT step_id, run_id, step_index, name, status, detail, created_at FROM run_steps WHERE run_id = ? ORDER BY step_index",
            (run_id,),
        ).fetchall()
    value = dict(run)
    for key in ("intent", "context_manifest", "steps", "response"):
        value[key] = json_loads(value.get(key), {} if key in {"intent", "context_manifest", "response"} else [])
    value["publication_id"] = value["context_manifest"].get("knowledge_publication_id")
    value["steps"] = [dict(step) for step in steps] or value["steps"]
    return value


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _scenario_for_item(slug: str) -> dict[str, Any] | None:
    mapping = {
        "venue-application": "scenario-venue-v1",
        "course-selection": "scenario-course-v1",
        "work-study": "scenario-workstudy-v1",
        "leave-application": "scenario-leave-v1",
    }
    scenario_id = mapping.get(slug)
    return get_scenario(scenario_id) if scenario_id else None


def _freshness_policy(item: dict[str, Any], evidence: list[dict[str, Any]], query: str) -> tuple[str, str | None]:
    state = evidence[0]["freshness_state"] if evidence else "no_evidence"
    current_question = any(word in query for word in ("现在", "当前", "还能", "截止", "什么时候开始", "是否可以"))
    if state in {"possibly_stale", "pending_review", "conflicted", "temporarily_unavailable", "withdrawn", "no_evidence"}:
        return "caution", "当前依据需要进一步核验，系统不会把它表述为确定有效。"
    if current_question and item["risk_class"] == "time_sensitive":
        return "caution", "涉及时间窗口或个人业务状态，请以官方系统当前显示为准。"
    return "verified", None


def _clarifications_for(item: dict[str, Any], query: str) -> list[dict[str, str]]:
    """Return only fields needed to decide the next deterministic rule."""
    if item["slug"] != "venue-application":
        return []

    informational_request = (
        any(phrase in query for phrase in ("材料", "准备什么", "什么时候", "能不能", "区别", "怎么发布", "入口"))
        or ("可以" in query and "吗" in query)
        or ("是否" in query)
    )
    action_request = any(phrase in query for phrase in ("申请", "预约", "预定", "借教室", "办活动", "办一场", "使用场地"))
    if informational_request or not action_request:
        return []

    clarifications: list[dict[str, str]] = []
    has_date = bool(
        re.search(
            r"(?:\d{1,4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{1,2}\s*月\s*\d{1,2}\s*日|"
            r"\d{4}[-/.]\d{1,2}(?:[-/.]\d{1,2})?|周[一二三四五六日天]|星期[一二三四五六日天])",
            query,
        )
    )
    has_time = bool(re.search(r"\d{1,2}\s*[:：点时]\s*\d{0,2}|上午|下午|晚上|早上|全天", query))
    if not has_date:
        clarifications.append({"field": "date", "question": "计划哪一天使用场地？"})
    if not has_time:
        clarifications.append({"field": "start_time", "question": "计划使用的开始时间和结束时间是什么？"})
    return clarifications


def _answer_contract(
    item: dict[str, Any],
    evidence: list[dict[str, Any]],
    query: str,
    trace_id: str,
    publication_id: str | None,
    agent_draft: dict[str, Any] | None = None,
    validated_claims: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    scenario = _scenario_for_item(item["slug"])
    freshness, caution = _freshness_policy(item, evidence, query)
    task_available = scenario is not None and item["slug"] == "venue-application"
    clarifications = _clarifications_for(item, query)
    boundary_label = "场景还原 · 演示数据 · 模拟办理" if task_available else "演示信息 · 当前未接入学校真实业务系统"
    response_kind = "clarification" if clarifications else "service_card"
    next_action = "ask_user" if clarifications else "preview_simulation" if task_available else "open_official_entry"
    evidence_slugs = {entry.get("slug") for entry in evidence}
    draft_claims = (
        [{"text": claim["text"]} for claim in validated_claims if claim.get("text")]
        if validated_claims is not None
        else [
            {"text": claim["text"]}
            for claim in (agent_draft or {}).get("claims", [])
            if isinstance(claim, dict)
            and claim.get("text")
            and set(claim.get("evidence_refs") or []).issubset(evidence_slugs)
        ]
    )
    claims = draft_claims or [{"text": item["summary"]}]
    draft_next_actions = [
        action.strip()
        for action in (agent_draft or {}).get("next_actions", [])
        if isinstance(action, str) and action.strip()
    ][:5]
    next_actions = draft_next_actions or (["补充上面列出的必要信息"] if clarifications else ["启动模拟任务"] if task_available else ["打开官方核验入口"])
    return {
        "response_id": trace_id,
        "response_kind": response_kind,
        "kind": "clarify" if clarifications else "answer",
        "status": "clarify" if clarifications else "answer",
        "title": item["title"],
        "summary": (agent_draft or {}).get("summary") or item["summary"],
        "service_item": {"id": item["id"], "slug": item["slug"], "title": item["title"], "domain": item["domain"], "icon": item["icon"]},
        "audience": item["audience"],
        "responsible_party": item["responsible_party"],
        "conditions": item["time_windows"],
        "materials": item["materials"],
        "steps": item["steps"],
        "official_entry": {"label": item["entry_label"], "url": item["entry_url"]},
        "evidence": evidence,
        "claims": claims,
        "citations": [entry["evidence_id"] for entry in evidence],
        "freshness": {"state": freshness, "label": "已核验演示依据" if freshness == "verified" else "需要核验", "message": caution, "checked_at": evidence[0].get("retrieved_at") if evidence else None},
        "simulation": task_available,
        "data_mode": "demo_simulation" if task_available else "demo_information",
        "simulation_boundary": {
            "simulation": task_available,
            "real_integration": False,
            "label": boundary_label,
        },
        "simulation_disclaimer": f"{boundary_label} · 当前未接入学校真实业务系统" if task_available else "{0}；不读取个人选课结果".format(boundary_label),
        "publication_id": publication_id,
        "scenario_id": scenario["id"] if scenario else None,
        "clarifications": clarifications,
        "next_action": next_action,
        "next_actions": next_actions,
        "trace_id": trace_id,
        "answer_draft": agent_draft,
    }


def _public_evidence_view(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep source details useful to students while dropping internal bindings."""
    return [
        {
            "title": student_business_text(item.get("title")),
            "source_title": student_business_text(item.get("source_title")),
            "source_url": item.get("source_url"),
            "published_at": item.get("published_at"),
            "retrieved_at": item.get("retrieved_at"),
            "freshness_state": item.get("freshness_state"),
        }
        for item in evidence
    ]


def student_response_view(response: dict[str, Any]) -> dict[str, Any]:
    """Project an internal response into the student-facing business contract."""
    view = {
        key: response.get(key)
        for key in (
            "kind",
            "response_kind",
            "status",
            "title",
            "summary",
            "audience",
            "responsible_party",
            "conditions",
            "materials",
            "steps",
            "official_entry",
            "freshness",
            "simulation",
            "simulation_boundary",
            "simulation_disclaimer",
            "claims",
            "clarifications",
            "next_action",
            "next_actions",
            "degradation",
        )
        if key in response
    }
    for key in ("title", "summary", "audience", "responsible_party", "simulation_disclaimer"):
        if key in view:
            view[key] = student_business_text(view[key])
    view["conditions"] = [student_business_text(value) for value in view.get("conditions") or []]
    view["materials"] = [student_business_text(value) for value in view.get("materials") or []]
    view["steps"] = [student_business_text(value) for value in view.get("steps") or []]
    view["next_actions"] = ["开始办理" if value == "启动模拟任务" else student_business_text(value) for value in view.get("next_actions") or []]
    if isinstance(view.get("freshness"), dict):
        view["freshness"] = {**view["freshness"], "label": student_business_text(view["freshness"].get("label"))}
    if isinstance(view.get("simulation_boundary"), dict):
        view["simulation_boundary"] = {**view["simulation_boundary"], "label": "当前未接入学校真实业务系统"}
    if "simulation_disclaimer" in view:
        boundary_notice = "当前页面使用脱敏、改写和虚拟化数据；当前未接入学校真实业务系统。"
        if response.get("simulation") is False:
            boundary_notice += "不读取个人选课结果。"
        view["simulation_disclaimer"] = boundary_notice
    service_item = response.get("service_item") or {}
    if service_item:
        view["service_item"] = {
            key: student_business_text(service_item.get(key)) if key in {"title", "domain"} else service_item.get(key)
            for key in ("title", "domain", "icon")
            if key in service_item
        }
    view["evidence"] = _public_evidence_view(response.get("evidence") or [])
    return view


def run_query(
    message: str,
    session_id: str | None,
    channel: str,
    task_id: str | None = None,
    retrieval_strategy: str = "c",
    publication_id: str | None = None,
    agent_config: AgentConfig | None = None,
    run_id: str | None = None,
    cancel_event: Event | None = None,
    streaming: bool = False,
) -> dict[str, Any]:
    trace_id = _new_id("trace")
    if run_id:
        trace_id = run_id
    _raise_if_cancelled(cancel_event)
    session_id = session_id or _new_id("session")
    started = now_iso()
    publication = get_current_publication() if not publication_id else get_publication(publication_id)
    if not publication:
        raise ValueError("publication_not_found")
    publication_id = publication.get("id")
    app_version = publication.get("app_version", "0.1.0-demo")
    retrieval_version = publication.get("retrieval_version", "retrieval-hybrid-v1")
    answer_contract_version = publication.get("answer_contract_version", "answer-card-v1")
    allow_pre_release = channel == "evaluation"
    retrieval_filters = {"simulation": True}
    if channel in {"standalone_web", "portal_sim", "wecom_sim"}:
        retrieval_filters["channel"] = channel
    try:
        agent_result = run_agent_loop(
            message,
            publication_id=publication_id,
            retrieval_strategy=retrieval_strategy,
            config=agent_config or AgentConfig(),
            search_fn=search,
            retrieval_filters=retrieval_filters,
            allow_pre_release=allow_pre_release,
            cancel_event=cancel_event,
            streaming=streaming,
        )
    except AgentModelError as error:
        if str(error) == "cancelled":
            raise RunCancelled("cancelled") from error
        raise
    _raise_if_cancelled(cancel_event)
    retrieval_query = agent_result.get("retrieval_query") or message
    retrieval_result = agent_result.get("retrieval_result")
    if not isinstance(retrieval_result, dict):
        retrieval_result = search(
            retrieval_query,
            publication_id=publication_id,
            strategy=retrieval_strategy,
            filters=retrieval_filters,
            allow_pre_release=allow_pre_release,
        )
    _raise_if_cancelled(cancel_event)
    candidates = retrieval_result["candidates"]
    evidence_validation = retrieval_result.get("meta", {}).get("evidence_validation") or {}
    evidence_gate_blocked = bool(candidates) and evidence_validation.get("valid") is not True
    answer_draft = agent_result.get("answer_draft") or {}
    answer_claims = answer_draft.get("claims") if isinstance(answer_draft, dict) else None
    if not isinstance(answer_claims, list) or not answer_claims:
        answer_claims = ([{"claim_id": candidates[0]["slug"], "text": candidates[0]["slug"], "evidence_refs": [candidates[0]["slug"]]}] if candidates else [])
    answer_validation = validate_claim_evidence(answer_claims, candidates[:3]) if candidates else {
        "valid": False,
        "support_status": "insufficient",
        "unsupported_claims": [],
        "claims": [],
    }
    retrieval_result.setdefault("meta", {})["answer_evidence_validation"] = answer_validation
    if candidates and answer_validation.get("valid") is not True:
        evidence_gate_blocked = True
    steps: list[dict[str, Any]] = []
    if agent_result.get("mode") == "api":
        steps.extend([
            {
                "name": "Agent Model Adapter",
                "status": "completed" if not agent_result.get("fallback") else "degraded",
                "detail": "服务端 Provider 配置已隔离；凭据和上游请求细节不进入持久化 Trace",
            },
            {
                "name": "Tool Router",
                "status": "completed" if agent_result.get("tool_calls") else "degraded",
                "detail": "只允许调用已注册的只读事项检索工具",
            },
            {
                "name": "Tool Observation",
                "status": "completed" if agent_result.get("tool_calls") else "degraded",
                "detail": "工具结果回写到有限上下文；事实仍由证据契约生成",
            },
        ])
    steps.extend([
        {"name": "意图识别", "status": "completed", "detail": "将自然语言目标映射到校园服务事项"},
        {"name": "元数据过滤", "status": "completed", "detail": "只使用已发布的演示来源版本"},
    ])
    retrieval_freshness = candidates[0]["freshness_state"] if candidates else "no_evidence"
    manifest_sections = [
        {"section_id": "section-instruction", "kind": "instruction", "source": "runtime-policy-v1", "source_ref": "runtime-policy-v1", "source_revision": None, "trust": "system", "trust_class": "system_rule", "priority": 100, "tokens": 90, "token_estimate": 90, "freshness_state": None, "cleanup_policy": "retain", "included_reason": "安全、模拟和回答契约"},
        {"section_id": "section-user", "kind": "user", "source": "current-request", "source_ref": "current-request", "source_revision": None, "trust": "user", "trust_class": "user_claim", "priority": 90, "tokens": max(8, len(message) // 2), "token_estimate": max(8, len(message) // 2), "freshness_state": None, "cleanup_policy": "retain", "included_reason": "当前目标"},
        {"section_id": "section-retrieval", "kind": "retrieval", "source": "published-evidence", "source_ref": "published-evidence", "source_revision": candidates[0]["source_revision_id"] if candidates else None, "trust": "published_source", "trust_class": "published_evidence", "priority": 80, "tokens": 120 * len(candidates), "token_estimate": 120 * len(candidates), "freshness_state": retrieval_freshness, "cleanup_policy": "fold_after_use", "included_reason": "按需装配当前事项证据"},
        {"section_id": "section-tool", "kind": "tool", "source": "tool-registry-v1", "source_ref": "tool-registry-v1", "source_revision": None, "trust": "controlled", "trust_class": "validated_tool_result", "priority": 70, "tokens": 60, "token_estimate": 60, "freshness_state": None, "cleanup_policy": "drop_after_step", "included_reason": "仅允许事项检索、规则预检和模拟任务"},
        {"section_id": "section-output-contract", "kind": "output_contract", "source": answer_contract_version, "source_ref": answer_contract_version, "source_revision": None, "trust": "contract", "trust_class": "system_rule", "priority": 100, "tokens": 80, "token_estimate": 80, "freshness_state": None, "cleanup_policy": "retain", "included_reason": "强制结论/材料/步骤/证据/边界字段"},
    ]
    intent = {"candidates": candidates[:3], "selected": candidates[0]["slug"] if candidates else None, "confidence": min(0.99, 0.55 + 0.08 * (candidates[0]["score"] if candidates else 0))}
    if evidence_gate_blocked:
        conflicted = evidence_validation.get("support_status") == "conflicted" or evidence_validation.get("conflict_status") == "detected"
        response = {
            "kind": "refused",
            "response_id": trace_id,
            "response_kind": "refusal",
            "status": "refuse",
            "title": "当前依据需要核验" if conflicted else "暂时没有足够依据",
            "summary": (
                "已找到相关事项，但当前已发布依据之间存在差异，系统不会替你选择其中一条作为确定规则。"
                if conflicted
                else "已找到可能相关的事项，但没有足够的可引用依据支持确定性回答。"
            ),
            "next_actions": ["打开官方核验入口", "补充更具体的事项或时间范围"],
            "evidence": [],
            "freshness": {"state": "caution", "label": "需要核验", "message": "系统不会把未通过证据校验的内容表述为确定规则。", "checked_at": None},
            "simulation": False,
            "data_mode": "demo_information",
            "simulation_boundary": {"simulation": False, "real_integration": False, "label": "当前未接入学校真实业务系统"},
            "simulation_disclaimer": "当前依据未通过核验，不启动办理流程",
            "citations": [],
            "next_action": "open_official_entry",
            "publication_id": publication_id,
            "trace_id": trace_id,
        }
        if conflicted:
            response["review_required"] = True
            response["freshness"]["state"] = "conflicted"
        steps.append({"name": "证据门", "status": "blocked", "detail": "候选依据未通过逐条 Claim 支持或冲突校验，拒绝生成确定性事项回答"})
    elif not candidates:
        response = {
            "kind": "refused",
            "response_id": trace_id,
            "response_kind": "refusal",
            "status": "refuse",
            "title": "暂时没有足够依据",
            "summary": "我没有在当前已发布的校园事项范围内找到可以核验的匹配项。请换一种说法，或直接进入学校官方服务目录核验。",
            "next_actions": ["补充你想办理的具体事务", "进入学校官方服务目录"],
            "evidence": [],
            "freshness": {"state": "no_evidence", "label": "无足够依据", "message": "系统不会根据常识编造校园政策或入口。", "checked_at": None},
            "simulation": False,
            "data_mode": "demo_information",
            "simulation_boundary": {"simulation": False, "real_integration": False, "label": "演示信息 · 当前未接入学校真实业务系统"},
            "simulation_disclaimer": "当前未接入学校真实业务系统",
            "citations": [],
            "next_action": "open_official_entry",
            "publication_id": publication_id,
            "trace_id": trace_id,
        }
        steps.append({"name": "证据门", "status": "blocked", "detail": "无已发布依据，拒绝生成确定性政策结论"})
    else:
        selected = get_item_by_slug(
            candidates[0]["slug"],
            publication_id=publication_id,
            allow_pre_release=allow_pre_release,
        )
        evidence = candidates[:3]
        response = _answer_contract(
            selected,
            evidence,
            message,
            trace_id,
            publication_id,
            agent_draft=agent_result.get("answer_draft"),
            validated_claims=answer_claims,
        )
        steps.extend([
            {"name": "按需检索证据", "status": "completed", "detail": f"召回 {len(evidence)} 条候选，使用当前发布版本"},
            {"name": "规则与安全护栏", "status": "completed", "detail": "验证来源状态、模拟边界和登录边界"},
            {"name": "事项卡输出", "status": "completed", "detail": "结构化返回可执行下一步和证据"},
        ])
    retrieval_state = retrieval_result.get("meta", {}).get("retrieval_state")
    if agent_result.get("status") in {"degraded", "budget_exceeded"} or retrieval_state == "degraded_sparse":
        response["degradation"] = {
            "state": "limited",
            "message": "当前使用已发布事项依据的关键词路径；智能回答或语义检索能力暂时受限，涉及当前变化请打开官方核验入口。",
            "recovery_action": "official_verify",
        }
    for index, step in enumerate(steps):
        step.setdefault("step_id", f"step-{trace_id}-{index + 1}")
        step["run_id"] = trace_id
    if task_id:
        steps.append({"name": "任务恢复", "status": "completed", "detail": f"恢复模拟办理任务 {task_id}"})
    manifest = {
        "context_manifest_id": _new_id("manifest"),
        "run_id": trace_id,
        "app_version": app_version,
        "knowledge_publication_id": publication_id,
        "publication_id": publication_id,
        "retrieval_version": retrieval_version,
        "answer_contract_version": answer_contract_version,
        "step_id": f"step-{trace_id}-context",
        "allowed_tools": registered_tool_ids(),
        "tool_calls": agent_result.get("tool_calls", []),
        "sections": manifest_sections,
        "total_token_estimate": sum(section["tokens"] for section in manifest_sections),
        "compaction_count": 0,
        "cleanup_policy": "保留结构、版本和引用；不保存密码、Cookie、令牌和个人业务记录",
        "assembled_at": started,
    }
    if run_id:
        manifest["stream_config"] = stream_config()
    feedback_ref = _new_id("feedback-ref")
    response.update({"session_id": session_id, "run_id": trace_id, "task_id": task_id})
    response["retrieval"] = retrieval_result["meta"]
    response["agent"] = {
        "mode": agent_result.get("mode", "deterministic"),
        "status": agent_result.get("status", "not_requested"),
        "turns": agent_result.get("turns", 0),
        "fallback": bool(agent_result.get("fallback")),
        "api_format": agent_result.get("api_format"),
        "protocol_attempts": list(agent_result.get("protocol_attempts") or []),
        "protocol_fallback": bool(agent_result.get("protocol_fallback")),
        "fallback_reason": agent_result.get("fallback_reason"),
        "disclaimer": "模型负责理解和选择受控工具；事项事实、规则和模拟状态由后端契约决定。",
    }
    run_status = (
        "needs_review"
        if response.get("review_required")
        else {"clarify": "waiting_user", "refused": "refused"}.get(response["kind"], "completed")
    )
    completed = now_iso()
    _raise_if_cancelled(cancel_event)
    with connect() as db:
        existing = db.execute("SELECT id FROM runs WHERE id = ?", (trace_id,)).fetchone()
        persisted_message = redact_text(message)
        values = (
            feedback_ref,
            session_id,
            channel,
            persisted_message,
            run_status,
            json_dumps(redact_value(intent)),
            json_dumps(redact_value(manifest)),
            json_dumps(redact_value(steps)),
            json_dumps(redact_value(response)),
            started,
            completed,
            agent_result.get("error_code"),
            trace_id,
        )
        if existing:
            db.execute(
                """UPDATE runs SET feedback_ref = ?, session_id = ?, channel = ?, input_text = ?, status = ?, intent = ?, context_manifest = ?, steps = ?, response = ?, created_at = ?, completed_at = ?, error_code = ? WHERE id = ?""",
                values,
            )
            db.execute("DELETE FROM run_steps WHERE run_id = ?", (trace_id,))
        else:
            db.execute(
                """INSERT INTO runs (id, feedback_ref, session_id, channel, input_text, status, intent, context_manifest, steps, response, created_at, completed_at, error_code)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trace_id,
                    feedback_ref,
                    session_id,
                    channel,
                    persisted_message,
                    run_status,
                    json_dumps(redact_value(intent)),
                    json_dumps(redact_value(manifest)),
                    json_dumps(redact_value(steps)),
                    json_dumps(redact_value(response)),
                    started,
                    completed,
                    agent_result.get("error_code"),
                ),
            )
        db.executemany(
            "INSERT INTO run_steps (step_id, run_id, step_index, name, status, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (step["step_id"], trace_id, index, step["name"], step["status"], step["detail"], completed)
                for index, step in enumerate(steps)
            ],
        )
        db.execute(
            "INSERT INTO audit_events VALUES (?, 'demo_student', ?, 'run', ?, ?, ?)",
            (_new_id("audit"), f"runtime.run.{run_status}", trace_id, json_dumps({"channel": channel, "simulation": response.get("simulation", False)}), completed),
        )
    return {
        "trace_id": trace_id,
        "feedback_ref": feedback_ref,
        "session_id": session_id,
        "response": response,
        "steps": steps,
        "context_manifest": manifest,
        "intent": intent,
        "retrieval": retrieval_result["meta"],
    }
