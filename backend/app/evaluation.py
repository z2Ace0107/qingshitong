from __future__ import annotations

import hashlib
import json
from threading import Thread
import uuid
from datetime import datetime, timezone
from typing import Any

from .db import connect, json_dumps, json_loads
from .knowledge import get_current_publication
from .publication import get_idempotent_result, get_publication, save_idempotent_result
from .redaction import redact_text, redact_value
from .runtime import run_query


DEFAULT_MODEL_PROFILE = "deterministic-no-llm-v1"
SUPPORTED_MODEL_PROFILES = {DEFAULT_MODEL_PROFILE}
SUPPORTED_ENVIRONMENT = "mock"
SUPPORTED_RUN_MODES = {"smoke", "regression", "release", "replay", "diagnostic"}
DEFAULT_RUN_MODE = "release"
EVALUATION_POLICY_VERSION = "r0-evaluation-policy-v1"
MAX_ATTEMPTS = 2
RETRYABLE_ERRORS = (TimeoutError, ConnectionError, OSError)
SUPPORTED_JUDGE_PROFILES = {"deterministic-semantic-v1"}
JUDGE_VERSION = "r0-judge-contract-v1"
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


CASES = [
    {"id": "core-01", "split": "golden", "input": "我想在示例活动广场办迎新活动，应该怎么办？", "expected_slug": "venue-application", "expected_kind": "answer"},
    {"id": "core-02", "split": "golden", "input": "活动场地申请要准备什么材料？", "expected_slug": "venue-application", "expected_kind": "answer"},
    {"id": "core-03", "split": "golden", "input": "选课什么时候开始？", "expected_slug": "course-selection", "expected_kind": "answer"},
    {"id": "core-04", "split": "golden", "input": "预选和正式选课有什么区别？", "expected_slug": "course-selection", "expected_kind": "answer"},
    {"id": "core-05", "split": "golden", "input": "有没有适合我时间的勤工助学岗位？", "expected_slug": "work-study", "expected_kind": "answer"},
    {"id": "core-06", "split": "golden", "input": "勤工助学申请需要什么？", "expected_slug": "work-study", "expected_kind": "answer"},
    {"id": "core-07", "split": "golden", "input": "帮我查一下我个人的选课结果", "expected_slug": "course-selection", "expected_kind": "answer"},
    {"id": "core-08", "split": "golden", "input": "我想办一个未知的校园事务", "expected_slug": None, "expected_kind": "refused"},
    {"id": "core-09", "split": "challenge", "input": "示例活动广场周末可以办活动吗？", "expected_slug": "venue-application", "expected_kind": "answer"},
    {"id": "core-10", "split": "challenge", "input": "岗位发布方怎么发布勤工助学岗位？", "expected_slug": "work-study", "expected_kind": "answer"},
    {"id": "core-11", "split": "challenge", "input": "请告诉我校园卡余额", "expected_slug": None, "expected_kind": "refused"},
    {"id": "core-12", "split": "holdout", "input": "我需要找教务系统课表入口", "expected_slug": "course-selection", "expected_kind": "answer"},
]


def _dataset_split_counts(cases: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for case in cases:
        split = case.get("split", "unassigned")
        counts[split] = counts.get(split, 0) + 1
    return counts


def _hash_payload(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _case_version(case: dict[str, Any]) -> str:
    return str(case.get("version", "v1"))


def _case_definition(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": case["id"],
        "version": _case_version(case),
        "split": case["split"],
        "input": case["input"],
        "expected_slug": case.get("expected_slug"),
        "expected_kind": case.get("expected_kind"),
        "execution": case.get("execution", "run"),
        "scorable": case.get("scorable", True),
        "skip_reason": case.get("skip_reason"),
        "source_refs": ["r0-evaluation-case-pack-v1"],
        "status": "approved",
    }


def _ensure_dataset_snapshot(dataset_version: str, publication: dict[str, Any]) -> str:
    if dataset_version != "core12-v1":
        raise ValueError("evaluation_dataset_not_supported")

    # The case pack is reusable, but its immutable dataset snapshot is tied to
    # the Publication locked by the Run. A candidate and the active bundle must
    # never collide on one dataset row.
    dataset_id = f"core12@{publication['id']}"
    created_at = now_iso()
    case_refs = [
        {"case_id": case["id"], "case_version": _case_version(case), "ordinal": index}
        for index, case in enumerate(CASES, start=1)
    ]
    dataset_definition = {
        "id": dataset_id,
        "version": dataset_version,
        "purpose": "r0_mixed",
        "case_refs": case_refs,
        "publication_id": publication["id"],
        "answer_contract_version": publication.get("answer_contract_version"),
        "source_refs": ["r0-evaluation-case-pack-v1"],
    }
    definition_hash = _hash_payload(dataset_definition)
    with connect() as db:
        for case in CASES:
            definition = _case_definition(case)
            case_hash = _hash_payload(definition)
            existing = db.execute(
                "SELECT definition_hash FROM evaluation_cases WHERE id = ? AND version = ?",
                (case["id"], _case_version(case)),
            ).fetchone()
            if existing and existing["definition_hash"] != case_hash:
                raise ValueError("evaluation_case_version_conflict")
            db.execute(
                """INSERT OR IGNORE INTO evaluation_cases
                (id, version, split, input_text, definition, source_refs, status, definition_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'approved', ?, ?)""",
                (
                    case["id"],
                    _case_version(case),
                    case["split"],
                    case["input"],
                    json_dumps(definition),
                    json_dumps(definition["source_refs"]),
                    case_hash,
                    created_at,
                ),
            )

        existing_dataset = db.execute(
            "SELECT definition_hash FROM evaluation_datasets WHERE id = ? AND version = ?",
            (dataset_id, dataset_version),
        ).fetchone()
        if existing_dataset and existing_dataset["definition_hash"] != definition_hash:
            raise ValueError("evaluation_dataset_version_conflict")
        db.execute(
            """INSERT OR IGNORE INTO evaluation_datasets
            (id, version, name, purpose, case_refs, publication_id, answer_contract_version, status,
             created_by, approved_by, created_at, approved_at, retired_at, change_note, source_refs, definition_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'approved', 'evaluation_engineer', 'business_knowledge_reviewer', ?, ?, NULL, ?, ?, ?)""",
            (
                dataset_id,
                dataset_version,
                "庆事通 R0 核心评测集",
                "r0_mixed",
                json_dumps(case_refs),
                publication["id"],
                publication.get("answer_contract_version", "unknown"),
                created_at,
                created_at,
                "固定脱敏案例版本；修改必须生成新版本",
                json_dumps(dataset_definition["source_refs"]),
                definition_hash,
            ),
        )
    return dataset_id


def _case_result(
    case: dict[str, Any],
    result: dict[str, Any],
    attempt_ids: list[str] | None = None,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    response = result["response"]
    actual_slug = (response.get("service_item") or {}).get("slug")
    retrieval_pass = actual_slug == case["expected_slug"]
    response_pass = response.get("kind") == case["expected_kind"]
    evidence_pass = bool(response.get("evidence")) if case["expected_slug"] else not response.get("evidence")
    boundary_pass = "模拟" in response.get("simulation_disclaimer", "") or not response.get("simulation", False)
    freshness_state = (response.get("freshness") or {}).get("state")
    freshness_pass = freshness_state in {"verified", "caution", "no_evidence"}
    legacy_statuses = {
        "retrieval": "pass" if retrieval_pass else "fail",
        "process": "pass" if response.get("next_action") else "error",
        "response": "pass" if response_pass and boundary_pass else "fail",
        "business": "pass" if evidence_pass and freshness_pass else "fail",
    }
    # L1-L4 are the formal R0 contract. The legacy names remain in the
    # persisted compatibility projection so old local reports can still be
    # read while new consumers use the stable layer labels.
    layer_statuses = {
        "L1": legacy_statuses["retrieval"],
        "L2": legacy_statuses["process"],
        "L3": "pass" if legacy_statuses["response"] == "pass" and legacy_statuses["business"] == "pass" else "fail",
        "L4": "pass" if response.get("trace_id") else "error",
    }
    overall = "pass" if all(value == "pass" for value in layer_statuses.values()) else "fail"
    if case.get("scorable", True) is False:
        overall = "unscored"
    program_assertions = [
        {"id": "service_item_match", "status": legacy_statuses["retrieval"], "reason": "事项候选与案例期望一致" if retrieval_pass else "事项候选与案例期望不一致"},
        {"id": "process_contract", "status": legacy_statuses["process"], "reason": "过程结果包含明确下一步" if response.get("next_action") else "过程结果缺少下一步"},
        {"id": "trace_present", "status": layer_statuses["L4"], "reason": "保留了运行 Trace" if response.get("trace_id") else "缺少运行 Trace"},
        {"id": "response_contract", "status": legacy_statuses["response"], "reason": "回答类型和边界满足案例契约" if response_pass and boundary_pass else "回答类型或边界不满足案例契约"},
        {"id": "evidence_freshness", "status": legacy_statuses["business"], "reason": "证据和时效状态满足案例契约" if evidence_pass and freshness_pass else "证据或时效状态不满足案例契约"},
    ]
    failure_categories = []
    if legacy_statuses["retrieval"] != "pass":
        failure_categories.append("routing" if actual_slug else "retrieval")
    if legacy_statuses["process"] != "pass":
        failure_categories.append("infrastructure")
    if legacy_statuses["response"] != "pass":
        failure_categories.append("response")
    if legacy_statuses["business"] != "pass":
        failure_categories.append("evidence")
    return {
        "result_id": f"result-{uuid.uuid4().hex[:12]}",
        "run_id": None,
        "case_id": case["id"],
        "case_version": _case_version(case),
        "split": case["split"],
        "input": case["input"],
        "expected": case,
        "actual": {
            "slug": actual_slug,
            "kind": response.get("kind"),
            "summary": response.get("summary", ""),
            "next_actions": response.get("next_actions", []),
            "publication_id": response.get("publication_id"),
            "simulation": response.get("simulation"),
            "simulation_disclaimer": response.get("simulation_disclaimer", ""),
            "freshness": response.get("freshness", {}),
        },
        "trace_id": result["trace_id"],
        "layers": legacy_statuses,
        "canonical_layers": layer_statuses,
        "legacy_layers": legacy_statuses,
        "layer_results": {
            "L1": {"status": layer_statuses["L1"], "label": "召回层", "checks": ["service_item_match"]},
            "L2": {"status": layer_statuses["L2"], "label": "过程层", "checks": ["process_contract"]},
            "L3": {"status": layer_statuses["L3"], "label": "结果层", "checks": ["response_contract", "evidence_freshness"]},
            "L4": {"status": layer_statuses["L4"], "label": "运行层", "checks": ["trace_present"], "trace_id": result["trace_id"]},
            # Compatibility aliases for existing local consumers. They are
            # derived from the canonical L1-L4 values and are not authoritative.
            "retrieval": {"status": legacy_statuses["retrieval"]},
            "process": {"status": legacy_statuses["process"]},
            "response": {"status": legacy_statuses["response"]},
            "business": {"status": legacy_statuses["business"]},
            "runtime": {"status": layer_statuses["L4"], "trace_id": result["trace_id"]},
        },
        "program_assertions": program_assertions,
        "judge_result_ids": [],
        "attempt_ids": attempt_ids or [],
        "human_review_ids": [],
        "failure_categories": failure_categories,
        "bad_case_id": None,
        "duration_ms": duration_ms,
        "status": overall,
        "unscored_reason": None if case.get("scorable", True) else "案例已运行，但当前没有足够的独立真值",
    }


def _skipped_result(case: dict[str, Any]) -> dict[str, Any]:
    reason = case.get("skip_reason") or "案例按数据集规则未执行"
    skipped_layers = {key: {"status": "skip", "reason": reason} for key in ("L1", "L2", "L3", "L4")}
    return {
        "result_id": f"result-{uuid.uuid4().hex[:12]}",
        "run_id": None,
        "case_id": case["id"],
        "case_version": _case_version(case),
        "split": case["split"],
        "input": case["input"],
        "expected": case,
        "actual": {},
        "trace_id": None,
        "layers": {"retrieval": "skip", "process": "skip", "response": "skip", "business": "skip"},
        "canonical_layers": {"L1": "skip", "L2": "skip", "L3": "skip", "L4": "skip"},
        "legacy_layers": {"retrieval": "skip", "process": "skip", "response": "skip", "business": "skip"},
        "layer_results": {
            **skipped_layers,
            **{key: {"status": "skip", "reason": reason} for key in ("retrieval", "process", "response", "business", "runtime")},
        },
        "program_assertions": [
            {"id": "execution", "status": "skip", "reason": reason},
            {"id": "evidence_freshness", "status": "skip", "reason": reason},
        ],
        "judge_result_ids": [],
        "attempt_ids": [],
        "human_review_ids": [],
        "failure_categories": [],
        "bad_case_id": None,
        "duration_ms": None,
        "status": "skip",
        "unscored_reason": None,
    }


def _error_result(case: dict[str, Any], error: Exception, attempt_ids: list[str], duration_ms: int | None = None) -> dict[str, Any]:
    error_code = type(error).__name__
    layers = {"L1": "error", "L2": "error", "L3": "error", "L4": "error"}
    return {
        "result_id": f"result-{uuid.uuid4().hex[:12]}",
        "run_id": None,
        "case_id": case["id"],
        "case_version": _case_version(case),
        "split": case["split"],
        "input": case["input"],
        "expected": case,
        "actual": {},
        "trace_id": None,
        "layers": {"retrieval": "error", "process": "error", "response": "error", "business": "error"},
        "canonical_layers": layers,
        "legacy_layers": {"retrieval": "error", "process": "error", "response": "error", "business": "error"},
        "layer_results": {
            **{key: {"status": "error", "error_code": error_code} for key in ("L1", "L2", "L3", "L4")},
            **{key: {"status": "error", "error_code": error_code} for key in ("retrieval", "process", "response", "business", "runtime")},
        },
        "program_assertions": [],
        "judge_result_ids": [],
        "attempt_ids": attempt_ids,
        "human_review_ids": [],
        "failure_categories": ["infrastructure"],
        "bad_case_id": None,
        "duration_ms": duration_ms,
        "error_code": error_code,
        "status": "error",
        "error": error_code,
    }


def _is_retryable(error: Exception) -> bool:
    return isinstance(error, RETRYABLE_ERRORS)


def _insert_attempt(
    run_id: str,
    case: dict[str, Any],
    attempt_id: str,
    attempt_no: int,
    started_at: str,
) -> None:
    with connect() as db:
        db.execute(
            """INSERT INTO evaluation_attempts
            (id, run_id, case_id, case_version, attempt_no, status, trace_id, error_code, retryable, payload, started_at, finished_at)
            VALUES (?, ?, ?, ?, ?, 'running', NULL, NULL, 0, '{}', ?, NULL)""",
            (attempt_id, run_id, case["id"], _case_version(case), attempt_no, started_at),
        )


def _finish_attempt(
    attempt_id: str,
    status: str,
    trace_id: str | None,
    error: Exception | None,
    retryable: bool,
    payload: dict[str, Any],
    finished_at: str,
) -> None:
    with connect() as db:
        db.execute(
            """UPDATE evaluation_attempts
            SET status = ?, trace_id = ?, error_code = ?, retryable = ?, payload = ?, finished_at = ?
            WHERE id = ?""",
            (
                status,
                trace_id,
                type(error).__name__ if error else None,
                1 if retryable else 0,
                json_dumps(payload),
                finished_at,
                attempt_id,
            ),
        )


def _persist_result(run_id: str, result: dict[str, Any], case: dict[str, Any], created_at: str) -> None:
    result["run_id"] = run_id
    with connect() as db:
        db.execute(
            """INSERT INTO evaluation_results
            (id, run_id, case_id, case_version, trace_id, status, split, expected, actual, layer_results,
             program_assertions, judge_result_ids, attempt_ids, human_review_ids, failure_categories, bad_case_id, duration_ms, error_code, token_estimate, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                result["result_id"],
                run_id,
                result["case_id"],
                result["case_version"],
                result.get("trace_id"),
                result["status"],
                result["split"],
                json_dumps(result.get("expected", case)),
                json_dumps(result.get("actual", {})),
                json_dumps(result.get("layer_results", {})),
                json_dumps(result.get("program_assertions", [])),
                json_dumps(result.get("judge_result_ids", [])),
                json_dumps(result.get("attempt_ids", [])),
                json_dumps(result.get("human_review_ids", [])),
                json_dumps(result.get("failure_categories", [])),
                result.get("bad_case_id"),
                result.get("duration_ms"),
                result.get("error_code"),
                result.get("token_estimate"),
                created_at,
            ),
        )


def _judge_result(case: dict[str, Any], result: dict[str, Any], judge_profile: str) -> dict[str, Any]:
    if judge_profile not in SUPPORTED_JUDGE_PROFILES:
        return {
            "judge_profile": judge_profile,
            "judge_version": JUDGE_VERSION,
            "status": "error",
            "score": None,
            "reason": "Judge 配置未实现，不能推断开放语义质量",
            "evidence_refs": [],
        }

    actual = result.get("actual", {})
    has_summary = bool(str(actual.get("summary", "")).strip())
    has_next_action = bool(actual.get("next_actions"))
    passed = has_summary and (case.get("expected_kind") != "answer" or has_next_action)
    return {
        "judge_profile": judge_profile,
        "judge_version": JUDGE_VERSION,
        "status": "pass" if passed else "fail",
        "score": 1.0 if passed else 0.0,
        "reason": "回答包含可理解的说明和下一步" if passed else "回答缺少可理解的说明或下一步",
        "evidence_refs": [],
    }


def _persist_judge_result(result_id: str, judge: dict[str, Any], created_at: str) -> str:
    judge_id = f"judge-{uuid.uuid4().hex[:12]}"
    with connect() as db:
        db.execute(
            """INSERT INTO evaluation_judge_results
            (id, result_id, judge_profile, judge_version, status, score, reason, evidence_refs, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                judge_id,
                result_id,
                judge["judge_profile"],
                judge["judge_version"],
                judge["status"],
                judge.get("score"),
                judge["reason"],
                json_dumps(judge.get("evidence_refs", [])),
                created_at,
            ),
        )
        row = db.execute("SELECT judge_result_ids FROM evaluation_results WHERE id = ?", (result_id,)).fetchone()
        ids = json_loads(row["judge_result_ids"], []) if row else []
        ids.append(judge_id)
        layer_row = db.execute("SELECT layer_results FROM evaluation_results WHERE id = ?", (result_id,)).fetchone()
        layer_results = json_loads(layer_row["layer_results"], {}) if layer_row else {}
        layer_results["judge"] = judge
        db.execute(
            "UPDATE evaluation_results SET judge_result_ids = ?, layer_results = ? WHERE id = ?",
            (json_dumps(ids), json_dumps(layer_results), result_id),
        )
    return judge_id


def _create_bad_case(case: dict[str, Any], result: dict[str, Any], publication_id: str) -> str:
    bad_case_id = f"bc-{uuid.uuid4().hex[:12]}"
    now = now_iso()
    category = (result.get("failure_categories") or ["evaluation"])[0]
    severity = "P1" if category in {"routing", "retrieval", "response", "rule", "safety"} else "P2"
    expected = {
        "case_id": case["id"],
        "case_version": _case_version(case),
        "kind": case.get("expected_kind"),
        "expected_slug": case.get("expected_slug"),
    }
    safe_expected = sanitize_bad_case_value(expected)
    safe_actual = sanitize_bad_case_value(result.get("actual", {}))
    sanitized_input = _sanitize_text(case["input"])
    with connect() as db:
        db.execute(
            """INSERT INTO bad_cases
            (id, origin, title, input_text, trace_id, category, severity, expected, actual, status,
             repair_note, created_at, updated_at, publication_id, regression_run_id, regression_result,
             close_note, candidate_fix_type, candidate_fix_ref, sanitized_input, owner_role)
            VALUES (?, 'evaluation', ?, ?, ?, ?, ?, ?, ?, 'open', NULL, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, 'evaluation_engineer')""",
            (
                bad_case_id,
                f"评测失败：{case['id']}",
                sanitized_input,
                result.get("trace_id"),
                category,
                severity,
                json_dumps(safe_expected),
                json_dumps(safe_actual),
                now,
                now,
                publication_id,
                sanitized_input,
            ),
        )
        db.execute(
            "INSERT INTO audit_events (id, actor_type, action, entity_type, entity_id, metadata, created_at) VALUES (?, 'evaluation_engineer', 'evaluation.bad_case.created', 'bad_case', ?, ?, ?)",
            (f"audit-{uuid.uuid4().hex[:12]}", bad_case_id, json_dumps({"case_id": case["id"], "trace_id": result.get("trace_id"), "publication_id": publication_id}), now),
        )
    return bad_case_id


def _sanitize_text(value: str) -> str:
    return redact_text(value)


def sanitize_bad_case_value(value: Any) -> Any:
    return redact_value(value)


def _load_normalized_results(run_id: str) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute("SELECT * FROM evaluation_results WHERE run_id = ? ORDER BY rowid", (run_id,)).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            for key in ("expected", "actual", "layer_results", "program_assertions", "judge_result_ids", "attempt_ids", "human_review_ids", "failure_categories"):
                value[key] = json_loads(value.get(key), [] if key.endswith("ids") or key in {"program_assertions", "failure_categories"} else {})
            assertion_status = {
                item.get("id"): item.get("status", "error")
                for item in value["program_assertions"]
                if isinstance(item, dict)
            }
            legacy_layers = {
                "retrieval": value["layer_results"].get("retrieval", {}).get("status", "error"),
                "process": value["layer_results"].get("process", {}).get("status", "error"),
                "response": value["layer_results"].get("response", {}).get("status", "error"),
                "business": value["layer_results"].get("business", {}).get("status", assertion_status.get("evidence_freshness", "error")),
            }
            value["canonical_layers"] = {
                "L1": value["layer_results"].get("L1", {}).get("status", legacy_layers["retrieval"]),
                "L2": value["layer_results"].get("L2", {}).get("status", legacy_layers["process"]),
                "L3": value["layer_results"].get("L3", {}).get("status", "pass" if legacy_layers["response"] == "pass" and legacy_layers["business"] == "pass" else "fail"),
                "L4": value["layer_results"].get("L4", {}).get("status", value["layer_results"].get("runtime", {}).get("status", "error")),
            }
            value["layers"] = legacy_layers
            value["legacy_layers"] = legacy_layers
            value["input"] = value["expected"].get("input", "") if isinstance(value["expected"], dict) else ""
            attempts = db.execute(
                "SELECT * FROM evaluation_attempts WHERE run_id = ? AND case_id = ? ORDER BY attempt_no",
                (run_id, value["case_id"]),
            ).fetchall()
            value["attempts"] = []
            for attempt in attempts:
                attempt_value = dict(attempt)
                attempt_value["payload"] = json_loads(attempt_value.get("payload"), {})
                value["attempts"].append(attempt_value)
            result.append(value)
    return result


def _summary(results: list[dict[str, Any]], run_mode: str) -> dict[str, Any]:
    counts = {status: sum(1 for item in results if item["status"] == status) for status in ("pass", "fail", "error", "skip", "unscored")}
    total = len(results)
    effective_denominator = counts["pass"] + counts["fail"]
    if run_mode in {"smoke", "diagnostic"}:
        quality_gate = "not_applicable"
    elif counts["error"] or counts["fail"]:
        quality_gate = "blocked"
    elif counts["skip"] or counts["unscored"]:
        quality_gate = "needs_review"
    else:
        quality_gate = "pass"
    return {
        "total": total,
        "counts": counts,
        "effective_denominator": effective_denominator,
        "excluded_counts": {"error": counts["error"], "skip": counts["skip"], "unscored": counts["unscored"]},
        "pass_rate": round(counts["pass"] / effective_denominator, 4) if effective_denominator else 0,
        "layer_pass_rates": {
            layer: round(sum((item.get("canonical_layers") or item.get("layers", {})).get(layer) == "pass" for item in results) / total, 4) if total else 0
            for layer in ("L1", "L2", "L3", "L4")
        },
        "retrieval_recall_at_1": round(sum((item.get("canonical_layers") or {}).get("L1") == "pass" for item in results) / total, 4) if total else 0,
        "evidence_support_rate": round(sum(item.get("legacy_layers", {}).get("business") == "pass" for item in results) / total, 4) if total else 0,
        "response_contract_rate": round(sum(item.get("legacy_layers", {}).get("response") == "pass" for item in results) / total, 4) if total else 0,
        "freshness_safety_rate": round(sum(item.get("legacy_layers", {}).get("business") == "pass" for item in results) / total, 4) if total else 0,
        "simulation_boundary_rate": round(sum("模拟" in (item.get("actual") or {}).get("simulation_disclaimer", "") or item.get("expected", {}).get("expected_slug") == "course-selection" for item in results) / total, 4) if total else 0,
        "dataset_splits": _dataset_split_counts(results),
        "quality_gate": quality_gate,
    }


def _run_control_status(run_id: str) -> str | None:
    with connect() as db:
        row = db.execute("SELECT status FROM evaluation_runs WHERE id = ?", (run_id,)).fetchone()
    return row["status"] if row else None


def _run_control_snapshot(run_id: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT status, version FROM evaluation_runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def _update_run_progress(run_id: str, results: list[dict[str, Any]], attempt_count: int, run_mode: str) -> None:
    with connect() as db:
        db.execute(
            "UPDATE evaluation_runs SET completed_cases = ?, attempt_count = ?, summary = ?, cases = ?, version = version + 1 WHERE id = ?",
            (len(results), attempt_count, json_dumps(_summary(results, run_mode)), json_dumps(results), run_id),
        )


def _execute_existing_run(
    run_id: str,
    manifest: dict[str, Any],
    selected_cases: list[dict[str, Any]],
) -> dict[str, Any]:
    run_mode = manifest["run_mode"] if "run_mode" in manifest else manifest.get("mode", DEFAULT_RUN_MODE)
    publication_id = manifest["publication_id"]
    existing = {item["case_id"]: item for item in _load_normalized_results(run_id)}
    results = list(existing.values())
    with connect() as db:
        row = db.execute("SELECT COUNT(*) AS count FROM evaluation_attempts WHERE run_id = ?", (run_id,)).fetchone()
    attempt_count = int(row["count"] if row else 0)

    for case in selected_cases:
        if case["id"] in existing:
            continue
        if _run_control_status(run_id) in {"paused", "cancel_requested", "cancelled"}:
            break

        if case.get("execution") == "skip":
            case_result = _skipped_result(case)
            _persist_result(run_id, case_result, case, now_iso())
            results.append(case_result)
            existing[case["id"]] = case_result
            _update_run_progress(run_id, results, attempt_count, run_mode)
            continue

        attempt_ids: list[str] = []
        case_result: dict[str, Any] | None = None
        with connect() as db:
            previous_attempt = db.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) AS attempt_no FROM evaluation_attempts WHERE run_id = ? AND case_id = ?",
                (run_id, case["id"]),
            ).fetchone()
        first_attempt_no = int(previous_attempt["attempt_no"] or 0) + 1
        for attempt_no in range(first_attempt_no, MAX_ATTEMPTS + 1):
            if _run_control_status(run_id) in {"paused", "cancel_requested", "cancelled"}:
                break
            attempt_id = f"attempt-{uuid.uuid4().hex[:12]}"
            attempt_ids.append(attempt_id)
            attempt_started = now_iso()
            _insert_attempt(run_id, case, attempt_id, attempt_no, attempt_started)
            attempt_count += 1
            try:
                result = run_query(
                    case["input"],
                    f"eval-session-{run_id}",
                    "evaluation",
                    publication_id=publication_id,
                )
            except Exception as error:
                retryable = _is_retryable(error)
                _finish_attempt(attempt_id, "error", None, error, retryable, {"message": "评测执行失败"}, now_iso())
                if retryable and attempt_no < MAX_ATTEMPTS:
                    continue
                case_result = _error_result(case, error, attempt_ids)
                break
            else:
                _finish_attempt(attempt_id, "completed", result.get("trace_id"), None, False, {"trace_id": result.get("trace_id")}, now_iso())
                case_result = _case_result(case, result, attempt_ids)
                break

        if case_result is None:
            if _run_control_status(run_id) in {"paused", "cancel_requested", "cancelled"}:
                break
            case_result = _error_result(case, RuntimeError("evaluation_attempt_missing"), attempt_ids)
        if case_result["status"] == "fail":
            case_result["bad_case_id"] = _create_bad_case(case, case_result, publication_id)
        _persist_result(run_id, case_result, case, now_iso())
        if manifest.get("judge_profile") and case_result["status"] not in {"error", "skip"}:
            judge = _judge_result(case, case_result, manifest["judge_profile"])
            judge_id = _persist_judge_result(case_result["result_id"], judge, now_iso())
            case_result["judge_result_ids"].append(judge_id)
            case_result["layer_results"]["judge"] = judge
        results.append(case_result)
        existing[case["id"]] = case_result
        _update_run_progress(run_id, results, attempt_count, run_mode)

    control_snapshot = _run_control_snapshot(run_id)
    current_status = control_snapshot["status"] if control_snapshot else None
    if current_status == "paused":
        final_status = "paused"
    elif current_status in {"cancel_requested", "cancelled"}:
        final_status = "partial" if results else "cancelled"
    elif len(results) == len(selected_cases):
        final_status = "completed"
    else:
        final_status = "partial"

    summary = _summary(results, run_mode)
    if len(results) < len(selected_cases) and run_mode not in {"smoke", "diagnostic"}:
        summary["quality_gate"] = "blocked"
        summary["incomplete_cases"] = len(selected_cases) - len(results)
    final_summary = json_dumps(summary)
    final_cases = json_dumps(results)
    final_finished_at = now_iso() if final_status in {"completed", "partial", "cancelled"} else None
    with connect() as db:
        current = db.execute(
            "SELECT status, summary, cases, completed_cases, attempt_count, finished_at, version FROM evaluation_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if current and control_snapshot and current["version"] == control_snapshot["version"] and (
            current["status"] != final_status
            or current["summary"] != final_summary
            or current["cases"] != final_cases
            or int(current["completed_cases"] or 0) != len(results)
            or int(current["attempt_count"] or 0) != attempt_count
            or current["finished_at"] != final_finished_at
        ):
            # Progress persistence can already have written the paused state.
            # Finalization must not create a second observable version between
            # a caller's read and its resume request, and must not overwrite a
            # concurrent control transition.
            db.execute(
                """UPDATE evaluation_runs
                   SET status = ?, summary = ?, cases = ?, completed_cases = ?,
                       attempt_count = ?, finished_at = ?, version = version + 1
                   WHERE id = ? AND version = ?""",
                (
                    final_status,
                    final_summary,
                    final_cases,
                    len(results),
                    attempt_count,
                    final_finished_at,
                    run_id,
                    control_snapshot["version"],
                ),
            )
    return get_evaluation(run_id) or {}


def _prepare_evaluation_run(
    dataset_version: str,
    runtime_profile: str,
    publication_id: str | None = None,
    model_profile: str = DEFAULT_MODEL_PROFILE,
    environment: str = SUPPORTED_ENVIRONMENT,
    run_mode: str = DEFAULT_RUN_MODE,
    parent_run_id: str | None = None,
    bad_case_id: str | None = None,
    judge_profile: str | None = None,
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    if environment != SUPPORTED_ENVIRONMENT:
        raise ValueError("evaluation_environment_not_supported")
    if model_profile not in SUPPORTED_MODEL_PROFILES:
        raise ValueError("evaluation_model_profile_not_supported")
    if run_mode not in SUPPORTED_RUN_MODES:
        raise ValueError("evaluation_run_mode_not_supported")
    run_id = f"eval-{uuid.uuid4().hex[:12]}"
    started = now_iso()
    publication = get_current_publication() if not publication_id else get_publication(publication_id)
    if not publication:
        raise ValueError("publication_not_found")
    if publication.get("status") not in {"ready", "active", "published", "superseded"}:
        raise ValueError("evaluation_publication_not_verified")
    publication_id = publication["id"]
    retrieval_version = publication["retrieval_version"]
    answer_contract_version = publication["answer_contract_version"]
    app_version = publication["app_version"]
    dataset_id = _ensure_dataset_snapshot(dataset_version, publication)
    selected_cases = CASES[:1] if run_mode == "smoke" else CASES
    if run_mode == "replay" and bad_case_id:
        with connect() as db:
            bad_case = db.execute("SELECT expected, trace_id, publication_id FROM bad_cases WHERE id = ?", (bad_case_id,)).fetchone()
        expected = json_loads(bad_case["expected"], {}) if bad_case else {}
        case_id = expected.get("case_id") if isinstance(expected, dict) else None
        case_version = expected.get("case_version") if isinstance(expected, dict) else None
        with connect() as db:
            source = None
            if bad_case and bad_case["trace_id"] and case_id and case_version:
                source = db.execute(
                    """SELECT er.run_id, er.case_id, er.case_version, er.trace_id,
                              evaluation_runs.dataset_version, evaluation_runs.publication_id,
                              evaluation_runs.manifest
                       FROM evaluation_results AS er
                       JOIN evaluation_runs ON evaluation_runs.id = er.run_id
                      WHERE er.trace_id = ? AND er.case_id = ? AND er.case_version = ?""",
                    (bad_case["trace_id"], case_id, case_version),
                ).fetchone()
        selected_cases = [
            case for case in CASES
            if case["id"] == case_id and _case_version(case) == case_version
        ]
        source_manifest = json_loads(source["manifest"], {}) if source else {}
        source_case_refs = source_manifest.get("case_refs", []) if isinstance(source_manifest, dict) else []
        source_case_locked = any(
            isinstance(ref, dict)
            and ref.get("case_id") == case_id
            and ref.get("case_version") == case_version
            for ref in source_case_refs
        )
        publication_matches = bool(
            source
            and source["publication_id"] == publication_id
            and (not bad_case["publication_id"] or bad_case["publication_id"] == source["publication_id"])
        )
        if (
            not bad_case
            or not source
            or source["dataset_version"] != dataset_version
            or not selected_cases
            or not source_case_locked
            or not publication_matches
        ):
            raise ValueError("bad_case_not_reproducible")
        parent_run_id = source["run_id"]
    manifest = {
        "manifest_version": "r0-evaluation-manifest-v1",
        "policy_version": EVALUATION_POLICY_VERSION,
        "dataset_id": dataset_id,
        "dataset_version": dataset_version,
        "case_refs": [
            {"case_id": case["id"], "case_version": _case_version(case), "ordinal": index}
            for index, case in enumerate(selected_cases, start=1)
        ],
        "publication_id": publication_id,
        "retrieval_version": retrieval_version,
        "embedding_profile": publication.get("embedding_profile") or publication.get("knowledge_model_version", "knowledge-model-v1"),
        "answer_contract_version": answer_contract_version,
        "runtime_profile": runtime_profile,
        "model_profile": model_profile,
        "judge_profile": judge_profile,
        "run_mode": run_mode,
        "bad_case_id": bad_case_id,
        "app_version": app_version,
        "environment": environment,
        "max_attempts": MAX_ATTEMPTS,
    }
    manifest_hash = _hash_payload(manifest)
    initial_summary = _summary([], run_mode)
    with connect() as db:
        db.execute(
            """INSERT INTO evaluation_runs
            (id, dataset_version, publication_id, retrieval_version, answer_contract_version, app_version,
             model_profile, runtime_profile, environment, status, summary, cases, started_at, finished_at,
             dataset_id, run_mode, embedding_profile, judge_profile, total_cases, completed_cases, attempt_count,
             parent_run_id, manifest, manifest_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, '[]', ?, NULL, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)""",
            (
                run_id,
                dataset_version,
                publication_id,
                retrieval_version,
                answer_contract_version,
                app_version,
                model_profile,
                runtime_profile,
                environment,
                json_dumps(initial_summary),
                started,
                dataset_id,
                run_mode,
                manifest["embedding_profile"],
                judge_profile,
                len(selected_cases),
                parent_run_id,
                json_dumps(manifest),
                manifest_hash,
            ),
        )
    manifest["run_mode"] = run_mode
    return run_id, manifest, selected_cases


def run_evaluation(
    dataset_version: str,
    runtime_profile: str,
    publication_id: str | None = None,
    model_profile: str = DEFAULT_MODEL_PROFILE,
    environment: str = SUPPORTED_ENVIRONMENT,
    run_mode: str = DEFAULT_RUN_MODE,
    parent_run_id: str | None = None,
    bad_case_id: str | None = None,
    judge_profile: str | None = None,
) -> dict[str, Any]:
    run_id, manifest, selected_cases = _prepare_evaluation_run(
        dataset_version,
        runtime_profile,
        publication_id,
        model_profile,
        environment,
        run_mode,
        parent_run_id,
        bad_case_id,
        judge_profile,
    )
    return _execute_existing_run(run_id, manifest, selected_cases)


def start_evaluation_run(
    dataset_version: str,
    runtime_profile: str,
    publication_id: str | None = None,
    model_profile: str = DEFAULT_MODEL_PROFILE,
    environment: str = SUPPORTED_ENVIRONMENT,
    run_mode: str = DEFAULT_RUN_MODE,
    parent_run_id: str | None = None,
    bad_case_id: str | None = None,
    judge_profile: str | None = None,
) -> dict[str, Any]:
    run_id, manifest, selected_cases = _prepare_evaluation_run(
        dataset_version,
        runtime_profile,
        publication_id,
        model_profile,
        environment,
        run_mode,
        parent_run_id,
        bad_case_id,
        judge_profile,
    )
    with connect() as db:
        db.execute("UPDATE evaluation_runs SET status = 'queued' WHERE id = ?", (run_id,))
    worker = Thread(target=_execute_existing_run, args=(run_id, manifest, selected_cases), name=f"qst-eval-{run_id}", daemon=True)
    worker.start()
    return get_evaluation(run_id) or {}


def control_evaluation_run(
    run_id: str,
    action: str,
    expected_version: int,
    idempotency_key: str,
    actor_role: str = "evaluation_engineer",
) -> dict[str, Any] | None:
    if action not in {"pause", "resume", "cancel"}:
        raise ValueError("evaluation_control_not_supported")
    operation = f"evaluation.run.{action}:{run_id}"
    cached = get_idempotent_result(operation, idempotency_key)
    if cached is not None:
        return cached
    with connect() as db:
        row = db.execute("SELECT * FROM evaluation_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        status = row["status"]
        current_version = int(row["version"] or 1)
        if current_version != expected_version:
            completed_cases = int(row["completed_cases"] or 0)
            total_cases = int(row["total_cases"] or 0)
            if action == "resume" and status in {"completed", "partial"} and completed_cases >= total_cases:
                return _evaluation_view(row)
            raise ValueError("evaluation_version_conflict")
        updated = 0
        next_status = status
        if action == "pause":
            if status == "paused":
                return _evaluation_view(row)
            if status not in {"queued", "running"}:
                raise ValueError("evaluation_pause_not_allowed")
            next_status = "paused"
            updated = db.execute(
                "UPDATE evaluation_runs SET status = ?, version = version + 1 WHERE id = ? AND version = ?",
                (next_status, run_id, expected_version),
            ).rowcount
        elif action == "cancel":
            if status in {"cancelled", "partial"}:
                return _evaluation_view(row)
            if status not in {"queued", "running", "paused"}:
                raise ValueError("evaluation_cancel_not_allowed")
            completed = int(row["completed_cases"] or 0)
            next_status = "partial" if completed else "cancelled"
            updated = db.execute(
                "UPDATE evaluation_runs SET status = ?, finished_at = ?, version = version + 1 WHERE id = ? AND version = ?",
                (next_status, now_iso(), run_id, expected_version),
            ).rowcount
        else:
            if status not in {"paused", "partial"}:
                raise ValueError("evaluation_resume_not_allowed")
            running_attempt = db.execute(
                "SELECT COUNT(*) AS count FROM evaluation_attempts WHERE run_id = ? AND status = 'running'",
                (run_id,),
            ).fetchone()["count"]
            if running_attempt:
                raise ValueError("evaluation_resume_not_allowed")
            next_status = "running"
            updated = db.execute(
                "UPDATE evaluation_runs SET status = 'running', finished_at = NULL, version = version + 1 WHERE id = ? AND version = ?",
                (run_id, expected_version),
            ).rowcount
        if updated != 1:
            latest = db.execute("SELECT * FROM evaluation_runs WHERE id = ?", (run_id,)).fetchone()
            if (
                action == "resume"
                and latest
                and latest["status"] in {"completed", "partial"}
                and int(latest["completed_cases"] or 0) >= int(latest["total_cases"] or 0)
            ):
                return _evaluation_view(latest)
            raise ValueError("evaluation_version_conflict")
        db.execute(
            "INSERT INTO audit_events (id, actor_type, action, entity_type, entity_id, metadata, created_at) VALUES (?, ?, ?, 'evaluation_run', ?, ?, ?)",
            (
                f"audit-{uuid.uuid4().hex[:12]}",
                actor_role,
                f"evaluation.run.{action}",
                run_id,
                json_dumps({"idempotency_key": idempotency_key, "expected_version": expected_version, "from_status": status, "to_status": next_status}),
                now_iso(),
            ),
        )
    if action == "resume":
        current = get_evaluation(run_id)
        if not current:
            return None
        manifest = current.get("manifest") or {}
        case_map = {case["id"]: case for case in CASES}
        selected_cases = [case_map[ref["case_id"]] for ref in manifest.get("case_refs", []) if ref.get("case_id") in case_map]
        result = _execute_existing_run(run_id, manifest, selected_cases)
    else:
        result = get_evaluation(run_id)
    if result is not None:
        save_idempotent_result(operation, idempotency_key, result)
    return result


def recover_incomplete_evaluations() -> int:
    """Move unfinished evaluation runs to an explicit resumable boundary after process restart."""
    recovered = 0
    recovered_at = now_iso()
    with connect() as db:
        rows = db.execute(
            "SELECT id, status, summary, cases FROM evaluation_runs WHERE status IN ('queued', 'running', 'cancel_requested')"
        ).fetchall()
    for row in rows:
        results = _load_normalized_results(row["id"])
        summary = json_loads(row["summary"], {})
        summary["recovered_after_restart"] = True
        summary["recovered_at"] = recovered_at
        summary["incomplete_cases"] = max(0, int(summary.get("total", 0)) - len(results))
        with connect() as db:
            db.execute(
                "UPDATE evaluation_runs SET status = 'partial', summary = ?, cases = ?, completed_cases = ?, finished_at = ?, version = version + 1 WHERE id = ? AND status IN ('queued', 'running', 'cancel_requested')",
                (json_dumps(summary), json_dumps(results), len(results), recovered_at, row["id"]),
            )
        recovered += 1
    return recovered


def list_evaluations(limit: int = 10) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute("SELECT * FROM evaluation_runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    result = []
    for row in rows:
        result.append(_evaluation_view(row))
    return result


def get_evaluation(run_id: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM evaluation_runs WHERE id = ?", (run_id,)).fetchone()
    return _evaluation_view(row) if row else None


def _evaluation_view(row: Any) -> dict[str, Any]:
    value = dict(row)
    value["summary"] = value.get("summary") if isinstance(value.get("summary"), dict) else json_loads(value.get("summary"), {})
    value["cases"] = value.get("cases") if isinstance(value.get("cases"), list) else json_loads(value.get("cases"), [])
    value["manifest"] = value.get("manifest") if isinstance(value.get("manifest"), dict) else json_loads(value.get("manifest"), {})
    normalized_results = _load_normalized_results(value["id"])
    value["results"] = normalized_results
    if normalized_results:
        value["cases"] = normalized_results
    value["knowledge_publication_id"] = value.get("publication_id")
    value["retrieval_index_version"] = value.get("retrieval_version")
    value["quality_gate_eligible"] = value.get("run_mode", DEFAULT_RUN_MODE) not in {"smoke", "diagnostic"}
    return value


def get_evaluation_result(result_id: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM evaluation_results WHERE id = ?", (result_id,)).fetchone()
        if not row:
            return None
        value = dict(row)
        for key in ("expected", "actual", "layer_results", "program_assertions", "judge_result_ids", "attempt_ids", "human_review_ids", "failure_categories"):
            value[key] = json_loads(value.get(key), [] if key.endswith("ids") or key in {"program_assertions", "failure_categories"} else {})
        value["attempts"] = []
        for attempt in db.execute("SELECT * FROM evaluation_attempts WHERE run_id = ? AND case_id = ? ORDER BY attempt_no", (value["run_id"], value["case_id"])).fetchall():
            attempt_value = dict(attempt)
            attempt_value["payload"] = json_loads(attempt_value.get("payload"), {})
            value["attempts"].append(attempt_value)
        value["judge_results"] = []
        for judge in db.execute("SELECT * FROM evaluation_judge_results WHERE result_id = ? ORDER BY created_at", (result_id,)).fetchall():
            judge_value = dict(judge)
            judge_value["evidence_refs"] = json_loads(judge_value.get("evidence_refs"), [])
            value["judge_results"].append(judge_value)
        value["human_reviews"] = [dict(review) for review in db.execute("SELECT * FROM evaluation_human_reviews WHERE result_id = ? ORDER BY created_at", (result_id,)).fetchall()]
        legacy_layers = {
            "retrieval": value["layer_results"].get("retrieval", {}).get("status", "error"),
            "process": value["layer_results"].get("process", {}).get("status", "error"),
            "response": value["layer_results"].get("response", {}).get("status", "error"),
            "business": value["layer_results"].get("business", {}).get("status", "error"),
        }
        value["canonical_layers"] = {
            "L1": value["layer_results"].get("L1", {}).get("status", legacy_layers["retrieval"]),
            "L2": value["layer_results"].get("L2", {}).get("status", legacy_layers["process"]),
            "L3": value["layer_results"].get("L3", {}).get("status", "pass" if legacy_layers["response"] == "pass" and legacy_layers["business"] == "pass" else "fail"),
            "L4": value["layer_results"].get("L4", {}).get("status", value["layer_results"].get("runtime", {}).get("status", "error")),
        }
        value["layers"] = legacy_layers
        value["legacy_layers"] = legacy_layers
    return value


def record_human_review(
    result_id: str,
    review_scope: str,
    reviewer_role: str,
    decision: str,
    note: str,
    *,
    expected_version: int,
    idempotency_key: str,
) -> dict[str, Any] | None:
    allowed_roles = {
        "technical": {"evaluation_engineer"},
        "business": {"business_knowledge_reviewer"},
    }
    if review_scope not in allowed_roles or reviewer_role not in allowed_roles[review_scope]:
        raise ValueError("governance_role_forbidden")
    if decision not in {"approve", "reject", "needs_review", "uncertain"}:
        raise ValueError("human_review_decision_not_supported")
    if decision == "needs_review":
        decision = "uncertain"
    safe_note = redact_text(note)
    operation = f"evaluation.result.human_review:{result_id}"
    cached = get_idempotent_result(operation, idempotency_key)
    if cached is not None:
        return cached
    with connect() as db:
        result_row = db.execute("SELECT id, human_review_ids, version FROM evaluation_results WHERE id = ?", (result_id,)).fetchone()
        if not result_row:
            return None
        current_version = int(result_row["version"] or 1)
        if current_version != expected_version:
            raise ValueError("evaluation_result_version_conflict")
        existing_ids = json_loads(result_row["human_review_ids"], [])
        review_id = f"review-{uuid.uuid4().hex[:12]}"
        created_at = now_iso()
        db.execute(
            """INSERT INTO evaluation_human_reviews
            (id, result_id, review_scope, reviewer_role, decision, note, judge_agreement, source_check, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                review_id,
                result_id,
                review_scope,
                reviewer_role,
                decision,
                safe_note,
                "not_run",
                "passed" if review_scope == "business" and decision == "approve" else "failed" if review_scope == "business" and decision == "reject" else "not_applicable",
                created_at,
            ),
        )
        db.execute(
            """INSERT INTO audit_events
            (id, actor_type, action, entity_type, entity_id, metadata, created_at)
            VALUES (?, ?, 'evaluation.human_review.recorded', 'evaluation_result', ?, ?, ?)""",
            (
                f"audit-{uuid.uuid4().hex[:12]}",
                reviewer_role,
                result_id,
                json_dumps(
                    {
                        "review_id": review_id,
                        "review_scope": review_scope,
                        "decision": decision,
                        "note": safe_note,
                        "expected_version": expected_version,
                        "idempotency_key": idempotency_key,
                    }
                ),
                created_at,
            ),
        )
        existing_ids.append(review_id)
        updated = db.execute(
            "UPDATE evaluation_results SET human_review_ids = ?, version = version + 1 WHERE id = ? AND version = ?",
            (json_dumps(existing_ids), result_id, expected_version),
        ).rowcount
        if updated != 1:
            raise ValueError("evaluation_result_version_conflict")
    result = get_evaluation_result(result_id)
    if result is None:
        return None
    review = result["human_reviews"][-1]
    save_idempotent_result(operation, idempotency_key, review)
    return review


def get_dashboard() -> dict[str, Any]:
    evaluations = list_evaluations()
    latest = evaluations[0] if evaluations else None
    with connect() as db:
        bad_cases = db.execute("SELECT * FROM bad_cases ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, updated_at DESC LIMIT 20").fetchall()
        feedback_total = db.execute("SELECT COUNT(*) AS count FROM feedback").fetchone()["count"]
        task_total = db.execute("SELECT COUNT(*) AS count FROM simulation_tasks").fetchone()["count"]
        run_total = db.execute("SELECT COUNT(*) AS count FROM runs").fetchone()["count"]
    return {
        "latest_evaluation": latest,
        "evaluations": evaluations,
        "bad_cases": [_bad_case_view(row) for row in bad_cases],
        "feedback_total": feedback_total,
        "task_total": task_total,
        "run_total": run_total,
        "dataset": {"version": "core12-v1", "cases": len(CASES), **_dataset_split_counts(CASES)},
    }


def _publication_gate_evidence(run_view: dict[str, Any] | None, db) -> dict[str, str]:
    """Read the independent Validator, Judge and human-review evidence for a run."""
    if not run_view:
        return {"validator_gate": "not_run", "judge_gate": "not_run", "human_review_gate": "not_run"}

    cases = run_view.get("cases") or []
    validator_statuses: list[str] = []
    judge_statuses: list[str] = []
    result_ids: list[str] = []
    for case in cases:
        result_id = case.get("id") or case.get("result_id")
        if result_id:
            result_ids.append(result_id)
        if case.get("status") == "error":
            validator_statuses.append("error")
        assertions = case.get("program_assertions") or []
        if assertions:
            validator_statuses.extend(
                assertion.get("status", "error")
                for assertion in assertions
                if isinstance(assertion, dict)
            )
        elif case.get("status") not in {"skip", "unscored"}:
            validator_statuses.append("error")

        judge = (case.get("layer_results") or {}).get("judge")
        if isinstance(judge, dict) and judge.get("status"):
            judge_statuses.append(judge["status"])

    def collapse_validator(statuses: list[str]) -> str:
        if not statuses:
            return "not_run"
        if "error" in statuses:
            return "error"
        if "fail" in statuses:
            return "fail"
        if all(status == "pass" for status in statuses):
            return "pass"
        return "not_run"

    def collapse_judge(statuses: list[str]) -> str:
        if not statuses:
            return "not_run"
        if "error" in statuses:
            return "error"
        if "fail" in statuses:
            return "fail"
        if "unscored" in statuses:
            return "unscored"
        if all(status == "pass" for status in statuses):
            return "pass"
        return "not_run"

    human_review_statuses: list[str] = []
    if result_ids:
        placeholders = ", ".join("?" for _ in result_ids)
        reviews = db.execute(
            f"SELECT decision FROM evaluation_human_reviews WHERE result_id IN ({placeholders})",
            result_ids,
        ).fetchall()
        human_review_statuses = [row["decision"] for row in reviews]
    if not human_review_statuses:
        human_review_gate = "not_run"
    elif any(decision in {"reject", "fail"} for decision in human_review_statuses):
        human_review_gate = "fail"
    elif any(decision in {"uncertain", "needs_review"} for decision in human_review_statuses):
        human_review_gate = "needs_review"
    else:
        human_review_gate = "pass"

    return {
        "validator_gate": collapse_validator(validator_statuses),
        "judge_gate": collapse_judge(judge_statuses),
        "human_review_gate": human_review_gate,
    }


def _calculate_publication_gate(
    publication_id: str,
    db,
    *,
    responsibility_gate_override: str | None = None,
    decided_by: str = "gate_calculator",
    gate_decision_id: str | None = None,
) -> dict[str, Any] | None:
    publication_row = db.execute("SELECT * FROM publications WHERE id = ?", (publication_id,)).fetchone()
    if not publication_row:
        return None
    publication = dict(publication_row)
    run = db.execute(
        """SELECT * FROM evaluation_runs
        WHERE publication_id = ?
          AND run_mode = 'release'
          AND status NOT IN ('queued', 'running')
        ORDER BY started_at DESC LIMIT 1""",
        (publication_id,),
    ).fetchone()
    bad_case_counts = db.execute(
        """SELECT severity, COUNT(*) AS count FROM bad_cases
        WHERE status NOT IN ('closed', 'rejected', 'wont_fix')
          AND (publication_id IS NULL OR publication_id = ?)
        GROUP BY severity""",
        (publication_id,),
    ).fetchall()

    p0_open_count = next((row["count"] for row in bad_case_counts if row["severity"] == "P0"), 0)
    p1_open_count = next((row["count"] for row in bad_case_counts if row["severity"] == "P1"), 0)
    run_view = _evaluation_view(run) if run else None
    gate_evidence = _publication_gate_evidence(run_view, db)
    cases = run_view["cases"] if run_view else []
    holdout_cases = [case for case in cases if case.get("split") == "holdout"]
    holdout_counts = {
        status: sum(case.get("status") == status for case in holdout_cases)
        for status in ("pass", "fail", "error", "skip", "unscored")
    }
    holdout_summary = {"total": len(holdout_cases), "counts": holdout_counts}

    fact_source_gate = publication.get("source_gate_result") or "not_run"
    responsibility_gate = responsibility_gate_override or publication.get("responsibility_gate_result") or "not_run"
    required_case_ids = {case["id"] for case in CASES if case.get("execution", "run") != "skip"}
    manifest_case_ids = {
        ref.get("case_id")
        for ref in (run_view or {}).get("manifest", {}).get("case_refs", [])
        if isinstance(ref, dict) and ref.get("case_id")
    }
    result_case_ids = {
        case.get("case_id")
        for case in cases
        if isinstance(case, dict) and case.get("case_id")
    }
    release_coverage_complete = bool(
        run_view
        and run_view.get("run_mode") == "release"
        and required_case_ids.issubset(manifest_case_ids)
        and required_case_ids.issubset(result_case_ids)
    )
    if not run_view:
        quality_gate = "not_run"
    else:
        counts = run_view.get("summary", {}).get("counts", {})
        incomplete = run_view.get("status") != "completed" or run_view.get("completed_cases", 0) < run_view.get("total_cases", 0)
        if counts.get("error", 0) or holdout_counts["error"]:
            quality_gate = "error"
        elif counts.get("fail", 0) or holdout_counts["fail"]:
            quality_gate = "fail"
        elif not release_coverage_complete or incomplete or counts.get("skip", 0) or counts.get("unscored", 0) or holdout_counts["skip"] or holdout_counts["unscored"]:
            quality_gate = "not_run"
        else:
            quality_gate = "pass"

    if gate_evidence["validator_gate"] == "fail":
        quality_gate = "fail"
    elif gate_evidence["validator_gate"] == "error":
        quality_gate = "error"
    elif gate_evidence["human_review_gate"] == "fail":
        quality_gate = "fail"
    elif gate_evidence["judge_gate"] == "error":
        quality_gate = "error"
    elif gate_evidence["judge_gate"] in {"fail", "unscored"} or gate_evidence["human_review_gate"] == "needs_review":
        if quality_gate == "pass":
            quality_gate = "needs_review"

    hard_block_gates = {"fail", "error", "not_run"}
    if p0_open_count or p1_open_count or any(gate in hard_block_gates for gate in (fact_source_gate, quality_gate, responsibility_gate)):
        decision = "block"
        reason = "存在未关闭的 P0/P1 Bad Case、失败门或未运行的必需检查，不能发布"
    elif any(gate != "pass" for gate in (fact_source_gate, quality_gate, responsibility_gate)):
        decision = "needs_review"
        reason = "事实源、质量或责任门尚未形成完整可验证证据"
    else:
        decision = "release"
        reason = "固定版本评测、Holdout 和三道门均通过"

    created_at = now_iso()
    decision_id = gate_decision_id or f"gate-{publication_id}-{run_view['id'] if run_view else 'not-run'}-{decided_by}"
    result = {
        "id": decision_id,
        "gate_decision_id": decision_id,
        "candidate_publication_id": publication_id,
        "evaluation_run_id": run_view["id"] if run_view else None,
        "fact_source_gate": fact_source_gate,
        "quality_gate": quality_gate,
        **gate_evidence,
        "responsibility_gate": responsibility_gate,
        "p0_open_count": p0_open_count,
        "p1_open_count": p1_open_count,
        "holdout_summary": holdout_summary,
        "decision": decision,
        "decided_by": decided_by,
        "decision_reason": reason,
        "created_at": created_at,
    }
    return result


def _persist_publication_gate_decision(db, result: dict[str, Any]) -> dict[str, Any]:
    """Append one release decision and its audit event without overwriting history."""
    db.execute(
        """INSERT INTO gate_decisions
        (id, candidate_publication_id, evaluation_run_id, fact_source_gate, quality_gate,
         validator_gate, judge_gate, human_review_gate, responsibility_gate,
         p0_open_count, p1_open_count, holdout_summary, decision,
         decided_by, decision_reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            result["id"], result["candidate_publication_id"], result["evaluation_run_id"],
            result["fact_source_gate"], result["quality_gate"], result["validator_gate"],
            result["judge_gate"], result["human_review_gate"], result["responsibility_gate"],
            result["p0_open_count"], result["p1_open_count"], json_dumps(result["holdout_summary"]),
            result["decision"], result["decided_by"], result["decision_reason"], result["created_at"],
        ),
    )
    db.execute(
        """INSERT INTO audit_events
        (id, actor_type, action, entity_type, entity_id, metadata, created_at)
        VALUES (?, ?, 'gate.decided', 'gate_decision', ?, ?, ?)""",
        (
            f"audit-{uuid.uuid4().hex[:12]}",
            result["decided_by"],
            result["id"],
            json_dumps(result),
            result["created_at"],
        ),
    )
    return result


def get_publication_gate(publication_id: str) -> dict[str, Any] | None:
    with connect() as db:
        return _calculate_publication_gate(publication_id, db)


def record_release_gate_decision(publication_id: str, connection) -> dict[str, Any] | None:
    """Record the release executor's gate decision in the publication transaction."""
    result = _calculate_publication_gate(
        publication_id,
        connection,
        responsibility_gate_override="pass",
        decided_by="release_executor",
        gate_decision_id=f"gate-{publication_id}-release-{uuid.uuid4().hex[:12]}",
    )
    if result is None:
        return None
    return _persist_publication_gate_decision(connection, result)


def _bad_case_view(row) -> dict[str, Any]:
    value = dict(row)
    for key in ("expected", "actual"):
        value[key] = json_loads(value.get(key), {})
    if "regression_result" in value:
        value["regression_result"] = json_loads(value.get("regression_result"), None)
    value = redact_value(value)
    value["sanitized_input"] = value.get("sanitized_input") or _sanitize_text(value.get("input_text", ""))
    value["version"] = int(value.get("version") or 1)
    return value


def create_bad_case(
    *,
    origin: str,
    input_text: str,
    sanitized_input: str | None,
    trace_id: str | None,
    publication_id: str | None,
    category: str,
    severity: str,
    expected: dict[str, Any],
    actual: dict[str, Any],
    actor_role: str,
    idempotency_key: str,
) -> dict[str, Any]:
    if actor_role not in {"evaluation_engineer", "business_knowledge_reviewer"}:
        raise ValueError("governance_role_forbidden")
    operation = f"evaluation.bad_case.create:{idempotency_key}"
    cached = get_idempotent_result(operation, idempotency_key)
    if cached is not None:
        return cached
    now = now_iso()
    safe_input = sanitize_bad_case_value(sanitized_input or input_text)
    safe_expected = sanitize_bad_case_value(expected)
    safe_actual = sanitize_bad_case_value(actual)
    status = "new"
    if trace_id and isinstance(expected, dict) and expected.get("case_id") and expected.get("case_version"):
        with connect() as db:
            source = db.execute(
                """SELECT er.run_id
                     FROM evaluation_results AS er
                     JOIN evaluation_runs ON evaluation_runs.id = er.run_id
                    WHERE er.trace_id = ?
                      AND er.case_id = ?
                      AND er.case_version = ?
                      AND (? IS NULL OR evaluation_runs.publication_id = ?)""",
                (
                    trace_id,
                    expected["case_id"],
                    expected["case_version"],
                    publication_id,
                    publication_id,
                ),
            ).fetchone()
        status = "reproducible" if source else "new"
    bad_case_id = f"bc-{uuid.uuid4().hex[:12]}"
    with connect() as db:
        db.execute(
            """INSERT INTO bad_cases
            (id, origin, title, input_text, trace_id, category, severity, expected, actual, status,
             repair_note, created_at, updated_at, publication_id, regression_run_id, regression_result,
             close_note, candidate_fix_type, candidate_fix_ref, sanitized_input, owner_role, version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, ?, 1)""",
            (
                bad_case_id,
                origin,
                f"人工记录：{category}",
                safe_input,
                trace_id,
                category,
                severity,
                 json_dumps(safe_expected),
                 json_dumps(safe_actual),
                status,
                now,
                now,
                publication_id,
                safe_input,
                actor_role,
            ),
        )
        db.execute(
            "INSERT INTO audit_events (id, actor_type, action, entity_type, entity_id, metadata, created_at) VALUES (?, ?, 'evaluation.bad_case.created', 'bad_case', ?, ?, ?)",
            (
                f"audit-{uuid.uuid4().hex[:12]}",
                actor_role,
                bad_case_id,
                json_dumps({"origin": origin, "trace_id": trace_id, "publication_id": publication_id, "status": status}),
                now,
            ),
        )
        row = db.execute("SELECT * FROM bad_cases WHERE id = ?", (bad_case_id,)).fetchone()
    result = _bad_case_view(row)
    save_idempotent_result(operation, idempotency_key, result)
    return result


def replay_bad_case(case_id: str, expected_version: int, idempotency_key: str) -> dict[str, Any] | None:
    operation = f"evaluation.bad_case.replay:{case_id}"
    cached = get_idempotent_result(operation, idempotency_key)
    if cached is not None:
        return cached
    with connect() as db:
        row = db.execute("SELECT * FROM bad_cases WHERE id = ?", (case_id,)).fetchone()
    if not row:
        return None
    if int(row["version"] or 1) != expected_version:
        raise ValueError("bad_case_version_conflict")
    expected = json_loads(row["expected"], {})
    if not row["trace_id"] or not isinstance(expected, dict) or not expected.get("case_id"):
        raise ValueError("bad_case_not_reproducible")
    result = run_evaluation(
        "core12-v1",
        "deterministic-agent-v1",
        publication_id=row["publication_id"] or None,
        run_mode="replay",
        parent_run_id=row["trace_id"],
        bad_case_id=case_id,
    )
    replay_status = result.get("status")
    replay_result = redact_value({
        "run_id": result.get("id"),
        "status": replay_status,
        "case_id": expected.get("case_id"),
        "original_trace_id": row["trace_id"],
        "publication_id": result.get("publication_id"),
        "manifest_hash": result.get("manifest_hash"),
        "completed_cases": result.get("completed_cases"),
    })
    now = now_iso()
    with connect() as db:
        updated = db.execute(
            "UPDATE bad_cases SET regression_run_id = ?, regression_result = ?, updated_at = ?, version = version + 1 WHERE id = ? AND version = ?",
            (result.get("id"), json_dumps(replay_result), now, case_id, expected_version),
        ).rowcount
        if updated != 1:
            raise ValueError("bad_case_version_conflict")
        db.execute(
            "INSERT INTO audit_events (id, actor_type, action, entity_type, entity_id, metadata, created_at) VALUES (?, 'evaluation_engineer', 'evaluation.bad_case.replayed', 'bad_case', ?, ?, ?)",
            (f"audit-{uuid.uuid4().hex[:12]}", case_id, json_dumps(redact_value(replay_result)), now),
        )
        current = db.execute("SELECT * FROM bad_cases WHERE id = ?", (case_id,)).fetchone()
    value = {"bad_case": _bad_case_view(current), "replay": result}
    save_idempotent_result(operation, idempotency_key, value)
    return value


def list_bad_cases() -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute("SELECT * FROM bad_cases ORDER BY updated_at DESC").fetchall()
    return [_bad_case_view(row) for row in rows]


def _audit_bad_case(db, action: str, case_id: str, metadata: dict[str, Any], created_at: str) -> None:
    db.execute(
        "INSERT INTO audit_events (id, actor_type, action, entity_type, entity_id, metadata, created_at) VALUES (?, 'evaluation_engineer', ?, 'bad_case', ?, ?, ?)",
        (f"audit-{uuid.uuid4().hex[:12]}", action, case_id, json_dumps(redact_value(metadata)), created_at),
    )


def repair_bad_case(
    case_id: str,
    repair_note: str,
    candidate_fix_type: str = "code",
    candidate_fix_ref: str | None = None,
) -> dict[str, Any] | None:
    now = now_iso()
    safe_repair_note = redact_text(repair_note)
    with connect() as db:
        current = db.execute("SELECT * FROM bad_cases WHERE id = ?", (case_id,)).fetchone()
        if not current:
            return None
        if current["status"] != "open":
            raise ValueError("bad_case_repair_not_allowed")
        db.execute(
            """UPDATE bad_cases
            SET status = 'fix_proposed', repair_note = ?, candidate_fix_type = ?,
                candidate_fix_ref = ?, updated_at = ?
            WHERE id = ?""",
            (safe_repair_note, candidate_fix_type, candidate_fix_ref, now, case_id),
        )
        _audit_bad_case(
            db,
            "bad_case.fix_proposed",
            case_id,
            {
                "repair_note": safe_repair_note,
                "candidate_fix_type": candidate_fix_type,
                "candidate_fix_ref": candidate_fix_ref,
            },
            now,
        )
        row = db.execute("SELECT * FROM bad_cases WHERE id = ?", (case_id,)).fetchone()
    return _bad_case_view(row) if row else None


def _response_matches_bad_case_contract(response: dict[str, Any], expected: dict[str, Any]) -> bool:
    expected_kind = expected.get("kind")
    if expected_kind and response.get("kind") != expected_kind:
        return False
    return all(response.get(field) not in (None, "", [], {}) for field in expected.get("must_show", []))


def regression_bad_case(case_id: str) -> dict[str, Any] | None:
    regression_run_id = f"regression-{uuid.uuid4().hex[:12]}"
    started = now_iso()
    with connect() as db:
        row = db.execute("SELECT * FROM bad_cases WHERE id = ?", (case_id,)).fetchone()
        if not row:
            return None
        if row["status"] != "fix_proposed":
            raise ValueError("bad_case_regression_not_allowed")
        original_run = db.execute("SELECT id, input_text, response, context_manifest FROM runs WHERE id = ?", (row["trace_id"],)).fetchone()
        if not original_run:
            raise ValueError("bad_case_not_reproducible")
        original_actual = json_loads(row["actual"], {})
        publication_id = row["publication_id"] or original_actual.get("publication_id") or json_loads(original_run["context_manifest"], {}).get("knowledge_publication_id")
        if not publication_id:
            raise ValueError("bad_case_not_reproducible")
        running_result = {
            "status": "running",
            "original_trace_id": row["trace_id"],
            "publication_id": publication_id,
            "started_at": started,
        }
        db.execute(
            "UPDATE bad_cases SET status = 'regression_running', regression_run_id = ?, regression_result = ?, updated_at = ? WHERE id = ?",
            (regression_run_id, json_dumps(running_result), started, case_id),
        )
        _audit_bad_case(db, "bad_case.regression_started", case_id, running_result, started)
        input_text = original_run["input_text"]
        expected = json_loads(row["expected"], {})

    passed = False
    regression: dict[str, Any] = {
        "status": "fail",
        "original_trace_id": row["trace_id"],
        "publication_id": publication_id,
        "regression_run_id": regression_run_id,
        "started_at": started,
    }
    try:
        replay = run_query(
            input_text,
            f"bad-case-regression-session-{regression_run_id}",
            "evaluation",
            publication_id=publication_id,
        )
        response = replay["response"]
        publication_match = response.get("publication_id") == publication_id
        passed = publication_match and _response_matches_bad_case_contract(response, expected)
        regression.update(
            {
                "status": "pass" if passed else "fail",
                "regression_trace_id": replay["trace_id"],
                "actual_kind": response.get("kind"),
                "expected_kind": expected.get("kind"),
                "publication_match": publication_match,
            }
        )
    except Exception as error:
        regression.update({"status": "error", "error": type(error).__name__})

    finished = now_iso()
    final_status = "verified" if passed else "fix_proposed"
    regression["finished_at"] = finished
    with connect() as db:
        db.execute(
            "UPDATE bad_cases SET status = ?, regression_result = ?, updated_at = ? WHERE id = ?",
            (final_status, json_dumps(regression), finished, case_id),
        )
        _audit_bad_case(db, "bad_case.regression_completed", case_id, {"status": regression["status"], "regression_run_id": regression_run_id}, finished)
        current = db.execute("SELECT * FROM bad_cases WHERE id = ?", (case_id,)).fetchone()
    result = _bad_case_view(current) if current else None
    if result is not None:
        result["regression"] = regression
    return result


def close_bad_case(case_id: str, close_note: str) -> dict[str, Any] | None:
    now = now_iso()
    safe_close_note = redact_text(close_note)
    with connect() as db:
        current = db.execute("SELECT * FROM bad_cases WHERE id = ?", (case_id,)).fetchone()
        if not current:
            return None
        if current["status"] != "verified":
            raise ValueError("bad_case_close_not_allowed")
        db.execute("UPDATE bad_cases SET status = 'closed', close_note = ?, updated_at = ? WHERE id = ?", (safe_close_note, now, case_id))
        _audit_bad_case(db, "bad_case.closed", case_id, {"close_note": safe_close_note}, now)
        row = db.execute("SELECT * FROM bad_cases WHERE id = ?", (case_id,)).fetchone()
    return _bad_case_view(row) if row else None
