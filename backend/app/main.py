from __future__ import annotations

import hashlib
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .agent import ProviderConfigurationError, load_provider_profile
from .db import connect, init_db, json_dumps, json_loads
from .embedding import EmbeddingConfigurationError
from .governance import (
    audit_session_action,
    create_session,
    current_session,
    require_csrf,
    require_session,
    revoke_session,
    rotate_csrf,
    set_session_cookie,
    session_view,
)
from .evaluation import close_bad_case, control_evaluation_run, create_bad_case, get_dashboard, get_evaluation, get_evaluation_result, get_publication_gate, list_bad_cases, list_evaluations, record_human_review, recover_incomplete_evaluations, replay_bad_case, regression_bad_case, repair_bad_case, run_evaluation, sanitize_bad_case_value, start_evaluation_run
from .knowledge import get_current_publication, get_item_by_slug, get_retrieval_health, get_scenario, get_source, list_items, list_scenarios, list_sources, student_bootstrap_view
from .schemas import (
    AdvanceRequest,
    BadCaseCreateRequest,
    BadCaseReplayRequest,
    BadCaseRepairRequest,
    ConfirmRequest,
    EvaluationRunRequest,
    EvaluationRunControlRequest,
    EvaluationHumanReviewRequest,
    FeedbackRequest,
    GovernanceAdvanceRequest,
    AnnouncementChangeRequest,
    BadCaseCloseRequest,
    LeaveCloseRequest,
    PublicationActivationRequest,
    PublicationBusinessReviewRequest,
    PublicationRollbackRequest,
    QueryRequest,
    ReconfirmationRequest,
    StudentReconfirmationRequest,
    SourceReviewRequest,
    TaskCreateRequest,
    TaskDataRequest,
    TaskMaterialsRequest,
    StudentTaskCreateRequest,
    WorkStudyJobCreateRequest,
    WorkStudyJobTransitionRequest,
    WorkStudyJobUpdateRequest,
)
from .seed import seed_demo_data
from .runtime import get_run, recover_incomplete_runs, request_stream_cancel, run_query, stream_query, student_response_view
from .student_session import set_student_session_cookie, student_session
from .simulation import advance_task, confirm_task, create_task, get_reconfirmation, get_student_reconfirmation, get_task, list_tasks, precheck_task, preview_task, reconfirm_pending_task, reconfirm_task, save_materials, student_preview_view, student_task_view
from .tooling import list_registered_tools
from .workstudy import create_job, get_job, list_jobs, transition_job, update_job
from .publication import (
    activate_publication,
    complete_business_review,
    get_idempotent_result,
    get_publication,
    list_publications,
    rollback_publication,
    review_revision,
    save_idempotent_result,
)


@asynccontextmanager
async def lifespan(application: FastAPI):
    # Fail closed for unsafe production secret conflicts before serving requests.
    application.state.provider_profile = load_provider_profile()
    init_db()
    seed_demo_data()
    recover_incomplete_runs()
    recover_incomplete_evaluations()
    yield


app = FastAPI(title="庆事通校园事务可信服务平台", version="0.1.0-demo", lifespan=lifespan)
origins = [item.strip() for item in __import__("os").getenv("QST_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if item.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False, allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(RequestValidationError)
def validation_error_handler(request, exc: RequestValidationError):
    """Return validation locations without echoing untrusted request values."""
    field_errors = [
        {
            "field": ".".join(str(part) for part in error.get("loc", []) if part != "body") or "request",
            "msg": error.get("msg"),
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "请求字段不符合要求",
                "retryable": False,
                "recovery_action": "edit_fields",
                "current_status": None,
                "field_errors": field_errors,
            }
        },
    )


def _current_provider_profile():
    profile = getattr(app.state, "provider_profile", None)
    if profile is None:
        profile = load_provider_profile()
        app.state.provider_profile = profile
    return profile


def _handle_value_error(error: ValueError) -> HTTPException:
    code = str(error)
    status = 404 if code.endswith("not_found") else 409 if code in {
        "invalid_transition",
        "confirmation_not_allowed",
        "task_not_ready_for_preview",
        "task_concurrent_update",
        "preview_expired",
        "reconfirmation_not_allowed",
        "material_update_not_allowed",
        "job_invalid_transition",
        "job_update_not_allowed",
        "job_concurrent_update",
        "leave_close_not_allowed",
        "bad_case_repair_not_allowed",
        "bad_case_regression_not_allowed",
        "bad_case_close_not_allowed",
        "bad_case_not_reproducible",
        "bad_case_version_conflict",
        "evaluation_publication_not_verified",
         "publication_build_failed",
         "business_review_not_allowed",
         "publication_activation_not_allowed",
         "source_revision_version_conflict",
         "publication_version_conflict",
         "publication_index_not_ready",
        "publication_gate_not_released",
        "evaluation_pause_not_allowed",
        "evaluation_resume_not_allowed",
        "evaluation_cancel_not_allowed",
        "evaluation_version_conflict",
        "evaluation_control_not_supported",
        "evaluation_result_version_conflict",
        "r0_scenario_not_available",
    } else 403 if code in {
        "governance_role_forbidden",
        "csrf_invalid",
        "governance_auth_required",
        "run_session_forbidden",
    } else 400
    return HTTPException(status_code=status, detail={"code": code, "message": "请求未能按当前状态执行"})


GOVERNANCE_READ_ROLES = {"evaluation_engineer", "business_knowledge_reviewer", "release_executor"}
EVALUATION_WRITE_ROLES = {"evaluation_engineer"}
KNOWLEDGE_REVIEW_ROLES = {"business_knowledge_reviewer"}
RELEASE_WRITE_ROLES = {"release_executor"}


def _require_governance(request: Request, roles: set[str], *, csrf: bool = False) -> dict[str, Any]:
    session = require_session(request, roles)
    if csrf:
        require_csrf(request, session)
    return session


def _require_idempotency_key(request: Request, *, message: str = "治理变更请求必须携带有效幂等键") -> str:
    value = request.headers.get("Idempotency-Key", "").strip()
    if len(value) < 4 or len(value) > 120:
        raise HTTPException(status_code=400, detail={"code": "idempotency_key_required", "message": message})
    return value


@app.get("/api/health/live")
def health_live():
    return {"status": "live", "service": "qingshitong-api"}


def _health_component(status: str, reason_code: str | None = None) -> dict[str, str | None]:
    return {"status": status, "reason_code": reason_code}


def _safe_binding(value: str | None) -> str | None:
    if not value:
        return None
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"


def _database_health() -> dict[str, str | None]:
    try:
        with connect() as db:
            result = db.execute("PRAGMA quick_check").fetchone()
            if not result or result[0] != "ok":
                return _health_component("unavailable", "database_integrity_check_failed")
            db.execute("SELECT 1").fetchone()
    except (Exception, sqlite3.Error):
        return _health_component("unavailable", "database_unavailable")
    return _health_component("ok")


def _publication_health() -> tuple[dict[str, str | None], str | None]:
    try:
        publication = get_current_publication()
    except (Exception, sqlite3.Error):
        return _health_component("unavailable", "publication_unavailable"), None
    if not publication or publication.get("status") not in {"active", "published"}:
        return _health_component("unavailable", "publication_missing"), None
    return _health_component("ok"), publication.get("id")


def _retrieval_health(publication_id: str | None) -> dict[str, str | None]:
    if not publication_id:
        return {
            **_health_component("unavailable", "publication_unavailable"),
            "_binding": {"publication": None, "keyword_index": None, "dense_index": None, "embedding_profile": None, "embedding_revision": None},
        }
    try:
        retrieval = get_retrieval_health(publication_id)
    except EmbeddingConfigurationError:
        return {
            **_health_component("unavailable", "retrieval_profile_invalid"),
            "_binding": {"publication": _safe_binding(publication_id), "keyword_index": None, "dense_index": None, "embedding_profile": None, "embedding_revision": None},
        }
    except (Exception, sqlite3.Error):
        return {
            **_health_component("unavailable", "retrieval_unavailable"),
            "_binding": {"publication": _safe_binding(publication_id), "keyword_index": None, "dense_index": None, "embedding_profile": None, "embedding_revision": None},
        }

    dense_profile = retrieval.get("dense_profile") or {}
    dense_index = retrieval.get("dense_index") or {}
    keyword_index = retrieval.get("keyword_index") or {}
    binding = {
        "publication": _safe_binding(publication_id),
        "keyword_index": _safe_binding(keyword_index.get("index_id")),
        "dense_index": _safe_binding(dense_index.get("index_id")),
        "embedding_profile": dense_profile.get("profile_id"),
        "embedding_revision": _safe_binding(dense_profile.get("model_revision")),
    }

    def component(status: str, reason_code: str | None = None) -> dict[str, Any]:
        return {**_health_component(status, reason_code), "_binding": binding}

    keyword_status = retrieval.get("keyword_index", {}).get("status")
    if keyword_status != "ready":
        reason = {
            "missing": "keyword_index_missing",
            "incompatible": "keyword_index_incompatible",
        }.get(keyword_status, "keyword_index_unavailable")
        return component("unavailable", reason)
    if retrieval.get("status") == "ready":
        return component("ok")
    return component("degraded", "dense_index_unavailable")


def _provider_health(profile=None) -> tuple[str, str | None]:
    try:
        profile = profile or _current_provider_profile()
    except ProviderConfigurationError:
        return "unavailable", "provider_configuration_invalid"
    if profile.configured:
        return "configured", None
    if profile.config_error:
        return "unavailable", "provider_configuration_invalid"
    return "not_configured", "provider_not_configured"


def _deterministic_core_health(publication_id: str | None) -> dict[str, str | None]:
    if not publication_id:
        return _health_component("unavailable", "publication_unavailable")
    try:
        item = get_item_by_slug("venue-application", publication_id=publication_id)
        scenario = get_scenario("scenario-venue-v1")
    except (Exception, sqlite3.Error):
        return _health_component("unavailable", "deterministic_core_unavailable")
    if not item:
        return _health_component("unavailable", "golden_service_item_unavailable")
    if not item.get("required_fields") or not item.get("materials") or not item.get("steps"):
        return _health_component("unavailable", "golden_service_contract_incomplete")
    if not scenario or scenario.get("status") != "published" or scenario.get("item_id") != item.get("id"):
        return _health_component("unavailable", "golden_scenario_unavailable")
    if not scenario.get("rules") or not scenario.get("steps"):
        return _health_component("unavailable", "golden_scenario_contract_incomplete")
    return _health_component("ok")


def _trace_health() -> dict[str, str | None]:
    required_tables = {"runs", "run_steps"}
    try:
        with connect() as db:
            rows = db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('runs', 'run_steps')"
            ).fetchall()
    except (Exception, sqlite3.Error):
        return _health_component("degraded", "trace_unavailable")
    present = {row[0] for row in rows}
    if required_tables - present:
        return _health_component("degraded", "trace_schema_missing")
    return _health_component("ok")


def _health_snapshot() -> dict[str, Any]:
    database = _database_health()
    if database["status"] != "ok":
        publication = _health_component("unavailable", "database_unavailable")
        publication_id = None
        retrieval = _health_component("unavailable", "database_unavailable")
    else:
        publication, publication_id = _publication_health()
        retrieval = _retrieval_health(publication_id if publication["status"] == "ok" else None)

    provider_profile = _current_provider_profile()
    provider_status, provider_reason = _provider_health(provider_profile)
    deterministic = _deterministic_core_health(publication_id if publication["status"] == "ok" else None)
    trace = _trace_health()
    core_ready = (
        database["status"] == "ok"
        and publication["status"] == "ok"
        and retrieval["status"] != "unavailable"
        and deterministic["status"] == "ok"
    )
    if not core_ready:
        status = "not_ready"
    elif provider_status != "configured" or retrieval["status"] == "degraded":
        status = "degraded"
    else:
        status = "ready"

    degraded_capabilities: list[str] = []
    if provider_status != "configured":
        degraded_capabilities.append("llm_answer")
    if retrieval["status"] == "degraded":
        degraded_capabilities.append("dense_retrieval")
    if deterministic["status"] != "ok":
        degraded_capabilities.append("deterministic_core")

    return {
        "status": status,
        "service": "qingshitong-api",
        "database": database["status"],
        "publication": publication["status"],
        "retrieval": retrieval["status"],
        "deterministic_core": deterministic["status"],
        "provider_status": provider_status,
        "degraded_capabilities": degraded_capabilities,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        # Kept for the existing student shell; it contains no governance detail.
        "message": "事项服务已就绪" if status == "ready" else "事项依据可用，智能回答能力受限" if status == "degraded" else "事项服务暂不可用",
        "capabilities": {
            "service_catalog": "available" if core_ready else "unavailable",
            "assisted_response": "available" if status == "ready" else "limited",
        },
        "_provider_reason": provider_reason,
        "_retrieval_reason": retrieval["reason_code"],
        "_deterministic_reason": deterministic["reason_code"],
        "_publication_binding": {"id": _safe_binding(publication_id if publication["status"] == "ok" else None)},
        "_retrieval_binding": retrieval.get("_binding", {}),
        "_provider_binding": {
            "provider": provider_profile.provider if provider_profile.configured else None,
            "model": provider_profile.model if provider_profile.configured else None,
            "protocol": provider_profile.protocol if provider_profile.configured else None,
        },
        "_trace": trace,
    }


@app.get("/api/health/ready")
def health_ready():
    snapshot = _health_snapshot()
    response_status = 503 if snapshot["status"] == "not_ready" else 200
    snapshot.pop("_provider_reason", None)
    snapshot.pop("_retrieval_reason", None)
    snapshot.pop("_deterministic_reason", None)
    snapshot.pop("_publication_binding", None)
    snapshot.pop("_retrieval_binding", None)
    snapshot.pop("_provider_binding", None)
    snapshot.pop("_trace", None)
    return JSONResponse(status_code=response_status, content=snapshot)


@app.get("/api/health/dependencies")
def health_dependencies():
    snapshot = _health_snapshot()
    return {
        "database": {"status": snapshot["database"], "reason_code": "database_unavailable" if snapshot["database"] == "unavailable" else None},
        "publication": {"status": snapshot["publication"], "reason_code": "publication_missing" if snapshot["publication"] == "unavailable" else None, "binding": snapshot["_publication_binding"]},
        "retrieval_index": {"status": snapshot["retrieval"], "reason_code": snapshot["_retrieval_reason"], "binding": snapshot["_retrieval_binding"]},
        "provider": {"status": snapshot["provider_status"], "reason_code": snapshot["_provider_reason"], "binding": snapshot["_provider_binding"]},
        "deterministic_core": {"status": snapshot["deterministic_core"], "reason_code": snapshot["_deterministic_reason"]},
        "worker": {"status": "not_configured", "reason_code": "r0_single_service"},
        "trace": snapshot["_trace"],
        "checked_at": snapshot["checked_at"],
    }


@app.get("/api/bootstrap")
def bootstrap():
    return {"publication": get_current_publication(), "items": list_items(), "scenarios": list_scenarios(), "dashboard": get_dashboard(), "disclaimer": "场景还原 · 演示数据 · 当前未接入学校真实业务系统"}


@app.get("/api/student/bootstrap")
def student_bootstrap():
    return student_bootstrap_view()


@app.post("/api/query")
def query(http_request: Request, request: QueryRequest):
    session_id, set_cookie = student_session(http_request)
    result = run_query(
        request.message.strip(),
        session_id,
        request.channel,
        request.task_id,
        "c",
        agent_config=_current_provider_profile().to_agent_config(),
    )
    response = JSONResponse(content={"response": student_response_view(result["response"]), "feedback_ref": result["feedback_ref"]})
    if set_cookie:
        set_student_session_cookie(response, session_id)
    return response


@app.post("/api/query/stream")
def query_stream(http_request: Request, request: QueryRequest):
    session_id, set_cookie = student_session(http_request)
    events = stream_query(
        request.message.strip(),
        session_id,
        request.channel,
        request.task_id,
        agent_config=_current_provider_profile().to_agent_config(),
    )
    response = StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
    if set_cookie:
        set_student_session_cookie(response, session_id)
    return response


@app.post("/api/runs/{run_id}/cancel")
def run_cancel(run_id: str, request: Request):
    idempotency_key = _require_idempotency_key(request, message="取消查询必须携带有效幂等键")
    session_id, _ = student_session(request, create=False)
    if not session_id:
        raise HTTPException(status_code=403, detail={"code": "run_session_forbidden", "message": "只能取消当前浏览器会话创建的查询"})
    try:
        return request_stream_cancel(run_id, idempotency_key, session_id=session_id)
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.get("/api/runtime/tools")
def runtime_tools():
    return {"tools": list_registered_tools(), "real_integration": False}


@app.post("/api/governance/session")
def governance_session(request: Request):
    """Create a role-bound local governance session from a deployment secret."""
    token = request.headers.get("X-QST-Governance-Token", "")
    session = create_session(token)
    response = JSONResponse(content={"session": session_view(session, session.pop("csrf_token"))})
    set_session_cookie(response, session["session_id"])
    return response


@app.get("/api/governance/session")
def governance_session_get(request: Request):
    session = require_session(request)
    csrf_token = rotate_csrf(session)
    return {"session": session_view(session, csrf_token)}


@app.delete("/api/governance/session")
def governance_session_delete(request: Request):
    session = require_session(request)
    require_csrf(request, session)
    revoke_session(session["id"])
    response = JSONResponse(content={"status": "signed_out"})
    response.delete_cookie("qst_governance_session", path="/api/governance")
    return response


@app.get("/api/runs/{run_id}")
def run_get(run_id: str):
    raise HTTPException(status_code=403, detail={"code": "governance_auth_required", "message": "该运行记录仅供受控治理会话查看"})


@app.get("/api/governance/runs/{run_id}")
def governance_run_get(run_id: str, request: Request):
    _require_governance(request, GOVERNANCE_READ_ROLES)
    value = get_run(run_id)
    if not value:
        raise HTTPException(status_code=404, detail={"code": "run_not_found", "message": "运行记录不存在"})
    return value


@app.post("/api/feedback")
def feedback(request: FeedbackRequest):
    feedback_id = f"feedback-{uuid.uuid4().hex[:12]}"
    bad_case_id = None
    now = datetime.now(timezone.utc).isoformat()
    if request.rating == "not_helpful":
        bad_case_id = f"bc-{uuid.uuid4().hex[:12]}"
    with connect() as db:
        run = db.execute(
            "SELECT id, input_text, response FROM runs WHERE feedback_ref = ?",
            (request.response_id,),
        ).fetchone()
        if not run and request.trace_id:
            run = db.execute(
                "SELECT id, input_text, response FROM runs WHERE id = ?",
                (request.trace_id,),
            ).fetchone()
        if not run:
            raise HTTPException(status_code=404, detail={"code": "response_not_found", "message": "这条回答已不可用，请重新查询后再反馈"})
        trace_id = run["id"]
        if bad_case_id:
            input_text = sanitize_bad_case_value(run["input_text"])
            actual = sanitize_bad_case_value(json_loads(run["response"], {}))
            safe_reason = sanitize_bad_case_value(request.reason)
            safe_comment = sanitize_bad_case_value(request.comment)
            expected = {"kind": "answer", "must_show": ["evidence", "simulation_boundary", "next_actions"]}
            db.execute(
                """INSERT INTO bad_cases
                (id, origin, title, input_text, trace_id, category, severity, expected, actual, status, repair_note, created_at, updated_at, publication_id, regression_run_id, regression_result, close_note, candidate_fix_type, candidate_fix_ref, sanitized_input, owner_role)
                VALUES (?, 'user_feedback', ?, ?, ?, ?, 'P1', ?, ?, 'open', NULL, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, 'evaluation_engineer')""",
                (bad_case_id, safe_reason or "用户反馈未解决", input_text, trace_id, safe_reason or "quality", json_dumps(expected), json_dumps(actual), now, now, actual.get("publication_id"), input_text),
            )
        db.execute("INSERT INTO feedback VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (feedback_id, request.response_id, trace_id, request.rating, safe_reason if bad_case_id else sanitize_bad_case_value(request.reason), safe_comment if bad_case_id else sanitize_bad_case_value(request.comment), now, bad_case_id))
    return {"id": feedback_id, "bad_case_id": bad_case_id, "message": "反馈已进入质量治理队列" if bad_case_id else "感谢反馈，已记录本次体验"}


@app.get("/api/tasks")
def tasks(request: Request):
    _require_governance(request, GOVERNANCE_READ_ROLES)
    return {"tasks": list_tasks()}


@app.get("/api/student/tasks")
def student_tasks():
    return {
        "tasks": [
            student_task_view(task)
            for task in list_tasks()
            if (get_scenario(task.get("scenario_id")) or {}).get("slug") == "venue-application"
        ]
    }


def _require_r0_student_task(task_id: str) -> dict[str, Any]:
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": "task_not_found"})
    scenario = get_scenario(task.get("scenario_id")) or {}
    if scenario.get("slug") != "venue-application":
        raise HTTPException(status_code=409, detail={"code": "r0_scenario_not_available", "message": "当前 R0 只开放活动场地申请办理流程"})
    return task


@app.post("/api/student/tasks")
def student_tasks_create(request: StudentTaskCreateRequest):
    try:
        scenario = get_scenario(request.scenario_id)
        if not scenario or scenario.get("slug") != "venue-application":
            raise ValueError("r0_scenario_not_available")
        return student_task_view(create_task(request.scenario_id, request.channel, "demo_student", request.form_data))
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.get("/api/student/tasks/{task_id}")
def student_task_get(task_id: str):
    return student_task_view(_require_r0_student_task(task_id))


@app.post("/api/student/tasks/{task_id}/precheck")
def student_task_precheck(task_id: str, request: TaskDataRequest):
    try:
        _require_r0_student_task(task_id)
        reference_date = date.fromisoformat(request.reference_date) if request.reference_date else None
        return student_task_view(precheck_task(task_id, request.form_data, reference_date=reference_date))
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.put("/api/student/tasks/{task_id}/materials")
def student_task_materials(task_id: str, request: TaskMaterialsRequest):
    try:
        _require_r0_student_task(task_id)
        return student_task_view(save_materials(task_id, request.materials, request.expected_version, request.form_data))
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.post("/api/student/tasks/{task_id}/preview")
def student_task_preview(task_id: str):
    try:
        _require_r0_student_task(task_id)
        return student_preview_view(preview_task(task_id))
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.post("/api/student/tasks/{task_id}/confirm")
def student_task_confirm(task_id: str, request: ConfirmRequest):
    try:
        _require_r0_student_task(task_id)
        return student_task_view(
            confirm_task(
                task_id,
                request.idempotency_key,
                request.confirmed,
                expected_version=request.expected_version,
                preview_version=request.preview_version,
            )
        )
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.get("/api/student/tasks/{task_id}/reconfirmation")
def student_task_reconfirmation_view(task_id: str):
    try:
        _require_r0_student_task(task_id)
        return get_student_reconfirmation(task_id)
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.post("/api/student/tasks/{task_id}/reconfirmation")
def student_task_reconfirmation(task_id: str, http_request: Request, request: StudentReconfirmationRequest):
    idempotency_key = _require_idempotency_key(http_request, message="重新确认请求必须携带有效幂等键")
    try:
        _require_r0_student_task(task_id)
        return student_task_view(reconfirm_pending_task(task_id, request.confirmed, request.expected_version, idempotency_key))
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.post("/api/tasks")
def tasks_create(http_request: Request, request: TaskCreateRequest):
    session = _require_governance(http_request, GOVERNANCE_READ_ROLES, csrf=True)
    try:
        return create_task(request.scenario_id, request.channel, session["role"], request.form_data)
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.get("/api/tasks/{task_id}")
def task_get(task_id: str, request: Request):
    _require_governance(request, GOVERNANCE_READ_ROLES)
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": "task_not_found"})
    return task


@app.post("/api/tasks/{task_id}/precheck")
def task_precheck(task_id: str, http_request: Request, request: TaskDataRequest):
    _require_governance(http_request, GOVERNANCE_READ_ROLES, csrf=True)
    try:
        reference_date = None
        if request.reference_date:
            try:
                reference_date = date.fromisoformat(request.reference_date)
            except ValueError as error:
                raise ValueError("invalid_reference_date") from error
        return precheck_task(task_id, request.form_data, reference_date=reference_date)
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.put("/api/tasks/{task_id}/materials")
def task_materials_save(task_id: str, http_request: Request, request: TaskMaterialsRequest):
    _require_governance(http_request, GOVERNANCE_READ_ROLES, csrf=True)
    try:
        return save_materials(task_id, request.materials, request.expected_version, request.form_data)
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.post("/api/tasks/{task_id}/preview")
def task_preview(task_id: str, request: Request):
    _require_governance(request, GOVERNANCE_READ_ROLES, csrf=True)
    try:
        return preview_task(task_id)
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.post("/api/tasks/{task_id}/confirm")
def task_confirm(task_id: str, http_request: Request, request: ConfirmRequest):
    _require_governance(http_request, GOVERNANCE_READ_ROLES, csrf=True)
    try:
        return confirm_task(
            task_id,
            request.idempotency_key,
            request.confirmed,
            expected_version=request.expected_version,
            preview_version=request.preview_version,
        )
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.get("/api/tasks/{task_id}/reconfirmation")
def task_reconfirmation_view(task_id: str, impact_event_id: str, request: Request):
    _require_governance(request, GOVERNANCE_READ_ROLES)
    try:
        return get_reconfirmation(task_id, impact_event_id)
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.post("/api/tasks/{task_id}/reconfirmation")
def task_reconfirmation(task_id: str, http_request: Request, request: ReconfirmationRequest):
    _require_governance(http_request, GOVERNANCE_READ_ROLES, csrf=True)
    try:
        return reconfirm_task(task_id, request.impact_event_id, request.confirmed, request.expected_version, request.idempotency_key)
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.post("/api/tasks/{task_id}/advance")
def task_advance(task_id: str, request: AdvanceRequest):
    raise HTTPException(
        status_code=403,
        detail={"code": "governance_endpoint_required", "message": "审核推进只能通过受控治理入口执行"},
    )


def _required_task_role(target_status: str) -> str:
    return {
        "under_simulated_review": "business_approver",
        "correction_required": "business_approver",
        "rejected_simulated": "business_approver",
        "approved_simulated": "business_approver",
        "awaiting_offline_confirmation": "business_approver",
        "archive_pending": "site_confirmation_recorder",
        "completed_simulated": "archive_operator",
        "leave_closed_simulated": "archive_operator",
    }.get(target_status, "business_approver")


@app.get("/api/governance/tasks/{task_id}")
def governance_task_get(task_id: str, request: Request):
    session = require_session(request)
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": "task_not_found"})
    return {"task": task, "governance": {"role": session["role"], "session_bound": True}}


@app.post("/api/governance/tasks/{task_id}/advance")
def governance_task_advance(task_id: str, request: Request, body: GovernanceAdvanceRequest):
    required_role = _required_task_role(body.target_status)
    session = require_session(request, {required_role})
    require_csrf(request, session)
    request_id = request.headers.get("X-Request-ID") or f"req-{uuid.uuid4().hex[:16]}"
    try:
        result = advance_task(
            task_id,
            body.target_status,
            "governance",
            reason=body.reason,
            expected_version=body.expected_version,
            idempotency_key=body.idempotency_key,
            actor_role=session["role"],
            governance_session_id=session["id"],
            request_id=request_id,
        )
    except ValueError as error:
        raise _handle_value_error(error) from error
    audit_session_action(
        session,
        "governance.task.advance_requested",
        "simulation_task",
        task_id,
        {"target_status": body.target_status, "request_id": request_id, "idempotency_key": body.idempotency_key},
    )
    return result


@app.post("/api/tasks/{task_id}/close")
def task_close(task_id: str, http_request: Request, request: LeaveCloseRequest):
    _require_governance(http_request, {"archive_operator"}, csrf=True)
    try:
        from .simulation import close_leave_task

        return close_leave_task(task_id, request.return_date, request.confirmed)
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.get("/api/workstudy/jobs")
def workstudy_jobs(actor_type: str = "demo_student"):
    if actor_type not in {"demo_student", "demo_provider"}:
        raise HTTPException(status_code=400, detail={"code": "invalid_actor_type"})
    return {"jobs": list_jobs(actor_type), "actor_type": actor_type, "simulation": True}


@app.get("/api/workstudy/jobs/{job_id}")
def workstudy_job(job_id: str, actor_type: str = "demo_student"):
    if actor_type not in {"demo_student", "demo_provider"}:
        raise HTTPException(status_code=400, detail={"code": "invalid_actor_type"})
    value = get_job(job_id, actor_type)
    if not value:
        raise HTTPException(status_code=404, detail={"code": "workstudy_job_not_found"})
    return value


@app.post("/api/workstudy/jobs")
def workstudy_job_create(request: WorkStudyJobCreateRequest):
    return create_job(request.model_dump())


@app.patch("/api/workstudy/jobs/{job_id}")
def workstudy_job_update(job_id: str, request: WorkStudyJobUpdateRequest):
    try:
        value = update_job(job_id, {key: value for key, value in request.model_dump().items() if key != "actor_type" and value is not None})
        if not value:
            raise HTTPException(status_code=404, detail={"code": "workstudy_job_not_found"})
        return value
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.post("/api/workstudy/jobs/{job_id}/transition")
def workstudy_job_transition(job_id: str, request: WorkStudyJobTransitionRequest):
    try:
        value = transition_job(job_id, request.target_status)
        if not value:
            raise HTTPException(status_code=404, detail={"code": "workstudy_job_not_found"})
        return value
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.post("/api/evaluations/run")
def evaluations_run(http_request: Request, request: EvaluationRunRequest):
    _require_governance(http_request, EVALUATION_WRITE_ROLES, csrf=True)
    try:
        return run_evaluation(
            request.dataset_version,
            request.runtime_profile,
            request.publication_id,
            request.model_profile,
            request.environment,
            request.run_mode,
            request.parent_run_id,
            request.bad_case_id,
            request.judge_profile,
        )
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.get("/api/evaluations")
def evaluations(request: Request):
    _require_governance(request, GOVERNANCE_READ_ROLES)
    return {"evaluations": list_evaluations()}


@app.post("/api/evaluations/runs")
def evaluation_runs_create(http_request: Request, request: EvaluationRunRequest):
    return evaluations_run(http_request, request)


@app.post("/api/governance/evaluations/runs", status_code=202)
def governance_evaluation_run_create(http_request: Request, request: EvaluationRunRequest):
    _require_governance(http_request, EVALUATION_WRITE_ROLES, csrf=True)
    try:
        return start_evaluation_run(
            request.dataset_version,
            request.runtime_profile,
            request.publication_id,
            request.model_profile,
            request.environment,
            request.run_mode,
            request.parent_run_id,
            request.bad_case_id,
            request.judge_profile,
        )
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.get("/api/evaluations/runs")
def evaluation_runs_list(request: Request):
    _require_governance(request, GOVERNANCE_READ_ROLES)
    return {"evaluations": list_evaluations()}


@app.get("/api/evaluations/runs/{run_id}")
def evaluation_run_get(run_id: str, request: Request):
    _require_governance(request, GOVERNANCE_READ_ROLES)
    value = get_evaluation(run_id)
    if not value:
        raise HTTPException(status_code=404, detail={"code": "evaluation_run_not_found"})
    return value


@app.get("/api/governance/evaluations/runs/{run_id}")
def governance_evaluation_run_get(run_id: str, request: Request):
    _require_governance(request, GOVERNANCE_READ_ROLES)
    value = get_evaluation(run_id)
    if not value:
        raise HTTPException(status_code=404, detail={"code": "evaluation_run_not_found"})
    return value


def _control_governance_evaluation_run(run_id: str, action: str, http_request: Request, request: EvaluationRunControlRequest):
    session = _require_governance(http_request, EVALUATION_WRITE_ROLES, csrf=True)
    idempotency_key = _require_idempotency_key(http_request)
    try:
        value = control_evaluation_run(run_id, action, request.expected_version, idempotency_key, session["role"])
        if not value:
            raise HTTPException(status_code=404, detail={"code": "evaluation_run_not_found"})
        return value
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.post("/api/governance/evaluations/runs/{run_id}/pause")
def governance_evaluation_run_pause(run_id: str, http_request: Request, request: EvaluationRunControlRequest):
    return _control_governance_evaluation_run(run_id, "pause", http_request, request)


@app.post("/api/governance/evaluations/runs/{run_id}/resume")
def governance_evaluation_run_resume(run_id: str, http_request: Request, request: EvaluationRunControlRequest):
    return _control_governance_evaluation_run(run_id, "resume", http_request, request)


@app.post("/api/governance/evaluations/runs/{run_id}/cancel")
def governance_evaluation_run_cancel(run_id: str, http_request: Request, request: EvaluationRunControlRequest):
    return _control_governance_evaluation_run(run_id, "cancel", http_request, request)


@app.get("/api/governance/evaluations/results/{result_id}")
def governance_evaluation_result_get(result_id: str, request: Request):
    _require_governance(request, GOVERNANCE_READ_ROLES)
    value = get_evaluation_result(result_id)
    if not value:
        raise HTTPException(status_code=404, detail={"code": "evaluation_result_not_found"})
    return value


@app.post("/api/governance/evaluations/results/{result_id}/human-review")
def governance_evaluation_result_review(result_id: str, http_request: Request, request: EvaluationHumanReviewRequest):
    session = _require_governance(http_request, EVALUATION_WRITE_ROLES | KNOWLEDGE_REVIEW_ROLES, csrf=True)
    idempotency_key = _require_idempotency_key(http_request)
    try:
        value = record_human_review(
            result_id,
            request.review_scope,
            session["role"],
            request.decision,
            request.note,
            expected_version=request.expected_version,
            idempotency_key=idempotency_key,
        )
        if not value:
            raise HTTPException(status_code=404, detail={"code": "evaluation_result_not_found"})
        return value
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.get("/api/dashboard")
@app.get("/api/governance/dashboard")
def dashboard(request: Request):
    _require_governance(request, GOVERNANCE_READ_ROLES)
    return get_dashboard()


@app.get("/api/bad-cases")
@app.get("/api/governance/bad-cases")
def bad_cases(request: Request):
    _require_governance(request, GOVERNANCE_READ_ROLES)
    return {"bad_cases": list_bad_cases()}


@app.post("/api/governance/bad-cases")
def governance_bad_case_create(http_request: Request, request: BadCaseCreateRequest):
    session = _require_governance(
        http_request,
        EVALUATION_WRITE_ROLES | KNOWLEDGE_REVIEW_ROLES,
        csrf=True,
    )
    idempotency_key = _require_idempotency_key(http_request)
    try:
        return create_bad_case(
            origin=request.origin,
            input_text=request.input_text,
            sanitized_input=request.sanitized_input,
            trace_id=request.trace_id,
            publication_id=request.publication_id,
            category=request.category,
            severity=request.severity,
            expected=request.expected,
            actual=request.actual,
            actor_role=session["role"],
            idempotency_key=idempotency_key,
        )
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.post("/api/governance/bad-cases/{case_id}/replay")
def governance_bad_case_replay(case_id: str, http_request: Request, request: BadCaseReplayRequest):
    _require_governance(http_request, EVALUATION_WRITE_ROLES, csrf=True)
    idempotency_key = _require_idempotency_key(http_request)
    try:
        value = replay_bad_case(case_id, request.expected_version, idempotency_key)
        if not value:
            raise HTTPException(status_code=404, detail={"code": "bad_case_not_found"})
        return value
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.post("/api/bad-cases/{case_id}/repair")
def bad_case_repair(case_id: str, http_request: Request, request: BadCaseRepairRequest):
    _require_governance(http_request, EVALUATION_WRITE_ROLES, csrf=True)
    try:
        value = repair_bad_case(case_id, request.repair_note, request.candidate_fix_type, request.candidate_fix_ref)
        if not value:
            raise HTTPException(status_code=404, detail={"code": "bad_case_not_found"})
        return value
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.post("/api/bad-cases/{case_id}/regression")
def bad_case_regression(case_id: str, request: Request):
    _require_governance(request, EVALUATION_WRITE_ROLES, csrf=True)
    try:
        value = regression_bad_case(case_id)
        if not value:
            raise HTTPException(status_code=404, detail={"code": "bad_case_not_found"})
        return value
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.post("/api/bad-cases/{case_id}/close")
def bad_case_close(case_id: str, http_request: Request, request: BadCaseCloseRequest):
    _require_governance(http_request, EVALUATION_WRITE_ROLES, csrf=True)
    try:
        value = close_bad_case(case_id, request.close_note)
        if not value:
            raise HTTPException(status_code=404, detail={"code": "bad_case_not_found"})
        return value
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.get("/api/releases/{publication_id}/gate")
@app.get("/api/governance/releases/{publication_id}/gate")
def release_gate(publication_id: str, request: Request):
    _require_governance(request, GOVERNANCE_READ_ROLES)
    value = get_publication_gate(publication_id)
    if not value:
        raise HTTPException(status_code=404, detail={"code": "publication_not_found"})
    return value


@app.get("/api/sources")
def sources(request: Request):
    _require_governance(request, GOVERNANCE_READ_ROLES)
    return {"sources": list_sources()}


@app.get("/api/publications")
def publications(request: Request):
    _require_governance(request, GOVERNANCE_READ_ROLES)
    return {"publications": list_publications()}


@app.get("/api/publications/{publication_id}")
def publication_get(publication_id: str, request: Request):
    _require_governance(request, GOVERNANCE_READ_ROLES)
    value = get_publication(publication_id)
    if not value:
        raise HTTPException(status_code=404, detail={"code": "publication_not_found"})
    return value


@app.post("/api/source-revisions/{revision_id}/review")
def source_revision_review(revision_id: str, http_request: Request, request: SourceReviewRequest):
    _require_governance(http_request, KNOWLEDGE_REVIEW_ROLES, csrf=True)
    idempotency_key = _require_idempotency_key(http_request)
    try:
        value = review_revision(
            revision_id,
            request.decision,
            request.review_note,
            request.expected_version,
            idempotency_key,
        )
        if value.get("status") == "not_found":
            raise HTTPException(status_code=404, detail={"code": "source_revision_not_found"})
        if value.get("status") == "checks_failed":
            raise HTTPException(status_code=409, detail=value)
        return value
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.post("/api/publications/{publication_id}/business-review")
def publication_business_review(publication_id: str, http_request: Request, request: PublicationBusinessReviewRequest):
    _require_governance(http_request, KNOWLEDGE_REVIEW_ROLES, csrf=True)
    idempotency_key = _require_idempotency_key(http_request)
    try:
        value = complete_business_review(
            publication_id,
            request.decision,
            request.review_note,
            request.expected_version,
            idempotency_key,
        )
        if value.get("status") == "not_found":
            raise HTTPException(status_code=404, detail={"code": "publication_not_found"})
        return value
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.post("/api/governance/releases/{publication_id}/release")
@app.post("/api/publications/{publication_id}/activate")
def publication_release(publication_id: str, http_request: Request, request: PublicationActivationRequest):
    _require_governance(http_request, RELEASE_WRITE_ROLES, csrf=True)
    idempotency_key = _require_idempotency_key(http_request)
    try:
        value = activate_publication(publication_id, request.release_note, request.expected_version, idempotency_key)
        if value.get("status") == "not_found":
            raise HTTPException(status_code=404, detail={"code": "publication_not_found"})
        return value
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.post("/api/publications/{publication_id}/rollback")
def publication_rollback(publication_id: str, http_request: Request, request: PublicationRollbackRequest):
    _require_governance(http_request, RELEASE_WRITE_ROLES, csrf=True)
    idempotency_key = _require_idempotency_key(http_request)
    try:
        value = rollback_publication(publication_id, request.reason, request.expected_version, idempotency_key)
        if value.get("status") == "not_found":
            raise HTTPException(status_code=404, detail={"code": "publication_not_found"})
        return value
    except ValueError as error:
        raise _handle_value_error(error) from error


@app.post("/api/demo/announcement-change")
def demo_announcement_change(http_request: Request, request: AnnouncementChangeRequest | None = None):
    """创建一条虚拟公告新版本，演示来源版本和任务重新评估链。"""
    _require_governance(http_request, EVALUATION_WRITE_ROLES, csrf=True)
    request = request or AnnouncementChangeRequest()
    source = get_source(request.source_id)
    if not source:
        raise HTTPException(status_code=404, detail={"code": "source_not_found"})
    if request.idempotency_key:
        cached = get_idempotent_result("demo.announcement_change", request.idempotency_key)
        if cached:
            return cached
    now = datetime.now(timezone.utc).isoformat()
    revision_id = f"{source['source_key']}-candidate-{uuid.uuid4().hex[:8]}"
    content = source["content"] + " 新版本演示变更：当前规则需要重新核对对应办理时间与材料。"
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    impact_id = f"impact-{uuid.uuid4().hex[:12]}"
    with connect() as db:
        db.execute(
            """INSERT INTO source_revisions
            (id, source_key, title, publisher, authority_type, url, content, content_hash,
             published_at, retrieved_at, effective_from, effective_to, status, freshness_state,
             supersedes_id, created_at, version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_review', 'pending_review', ?, ?, 1)""",
            (revision_id, source["source_key"], source["title"] + " · 新候选版本", source["publisher"], source["authority_type"], source["url"], content, content_hash, source.get("published_at"), now, source.get("effective_from"), source.get("effective_to"), source["id"], now),
        )
        db.execute(
            "INSERT INTO impact_events VALUES (?, ?, ?, 'high', ?, ?, ?, 'created', ?)",
            (impact_id, revision_id, json_dumps(["procedure_change", "freshness_change"]), json_dumps(["item-venue"] if request.source_id == "src-venue-v1" else ["item-course"]), "[]", "[]", now),
        )
    result = {"impact_event_id": impact_id, "candidate_revision_id": revision_id, "status": "pending_review", "affected_items": ["item-venue"] if request.source_id == "src-venue-v1" else ["item-course"], "affected_tasks": [], "message": "虚拟公告候选已创建；完成业务审核和发布切换后，系统才会标记受影响办理任务"}
    if request.idempotency_key:
        save_idempotent_result("demo.announcement_change", request.idempotency_key, result)
    return result


WEB_DIST = Path(__file__).resolve().parents[2] / "web"
if (WEB_DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")


@app.get("/{path:path}")
def frontend(path: str):
    if WEB_DIST.exists():
        candidate = WEB_DIST / path
        if path and candidate.exists() and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(WEB_DIST / "index.html")
    return {"service": "qingshitong", "message": "前端尚未构建，请运行 npm install && npm run build"}
