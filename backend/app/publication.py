from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from .db import connect, json_dumps, json_loads
from .knowledge import (
    build_keyword_index,
    build_selected_dense_index_if_available,
    detect_claim_conflicts,
    get_retrieval_health,
    set_knowledge_publication_status,
)
from .redaction import redact_text, redact_value


ACTIVE_PUBLICATION_STATUSES = {"active", "published"}
INDEXABLE_PUBLICATION_STATUSES = {
    "candidate",
    "building",
    "ready",
    "active",
    "published",
    "superseded",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _bindings(db, publication_id: str) -> dict[str, str]:
    rows = db.execute(
        "SELECT item_id, revision_id FROM publication_bindings WHERE publication_id = ? ORDER BY item_id",
        (publication_id,),
    ).fetchall()
    return {row["item_id"]: row["revision_id"] for row in rows}


def _publication_view(row, bindings: dict[str, str] | None = None) -> dict[str, Any] | None:
    if not row:
        return None
    value = dict(row)
    value["lifecycle_status"] = value.get("status")
    value["bindings"] = bindings or json_loads(value.get("source_bindings"), {})
    value["source_bindings"] = value["bindings"]
    return value


def get_publication(publication_id: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM publications WHERE id = ?", (publication_id,)).fetchone()
        bindings = _bindings(db, publication_id) if row else {}
    return _publication_view(row, bindings) if row else None


def list_publications(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute("SELECT * FROM publications ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [_publication_view(row, _bindings(db, row["id"])) for row in rows]


def _current(db):
    return db.execute(
        "SELECT * FROM publications WHERE status IN ('active', 'published') "
        "ORDER BY published_at DESC, created_at DESC LIMIT 1"
    ).fetchone()


def _record_status_transition(
    db,
    publication_id: str,
    from_status: str | None,
    to_status: str,
    now: str,
    actor_role: str = "business_knowledge_reviewer",
    **metadata: Any,
) -> None:
    db.execute(
        "INSERT INTO audit_events VALUES (?, ?, 'publication.status_changed', 'publication', ?, ?, ?)",
        (
            new_id("audit"),
            actor_role,
            publication_id,
            json_dumps(redact_value({"from": from_status, "to": to_status, **metadata})),
            now,
        ),
    )


def _get_idempotent_result(db, operation: str, idempotency_key: str) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT result FROM idempotency_records WHERE operation = ? AND idempotency_key = ?",
        (operation, idempotency_key),
    ).fetchone()
    return json_loads(row["result"], None) if row else None


def _save_idempotent_result(db, operation: str, idempotency_key: str, result: dict[str, Any]) -> None:
    db.execute(
        "INSERT OR IGNORE INTO idempotency_records (operation, idempotency_key, result, created_at) VALUES (?, ?, ?, ?)",
        (operation, idempotency_key, json_dumps(redact_value(result)), now_iso()),
    )


def _begin_write(db) -> None:
    db.execute("BEGIN IMMEDIATE")


def review_revision(
    revision_id: str,
    decision: str,
    review_note: str,
    expected_version: int,
    idempotency_key: str,
) -> dict[str, Any]:
    now = now_iso()
    review_note = redact_text(review_note)
    with connect() as db:
        _begin_write(db)
        operation = f"source_revision.review:{revision_id}"
        cached = _get_idempotent_result(db, operation, idempotency_key)
        if cached:
            return cached
        revision = db.execute("SELECT * FROM source_revisions WHERE id = ?", (revision_id,)).fetchone()
        if not revision:
            return {"status": "not_found", "revision_id": revision_id}
        revision = dict(revision)
        if revision["version"] != expected_version:
            raise ValueError("source_revision_version_conflict")
        if decision == "reject":
            if revision["status"] == "published":
                raise ValueError("revision_review_not_allowed")
            updated = db.execute(
                "UPDATE source_revisions SET status = 'rejected', freshness_state = 'possibly_stale', version = version + 1 "
                "WHERE id = ? AND version = ?",
                (revision_id, expected_version),
            )
            if updated.rowcount != 1:
                raise ValueError("source_revision_version_conflict")
            db.execute(
                "INSERT INTO audit_events VALUES (?, ?, 'source_revision.rejected', 'source_revision', ?, ?, ?)",
                (new_id("audit"), "business_knowledge_reviewer", revision_id, json_dumps(redact_value({"review_note": review_note, "expected_version": expected_version, "idempotency_key": idempotency_key})), now),
            )
            result = {"revision_id": revision_id, "status": "rejected", "publication_id": None, "review_note": review_note, "version": expected_version + 1}
            _save_idempotent_result(db, operation, idempotency_key, result)
            return result

        if revision["status"] not in {"pending_review", "approved"}:
            raise ValueError("revision_review_not_allowed")
        current = _current(db)
        if not current:
            raise ValueError("publication_not_found")
        item = db.execute(
            "SELECT id FROM service_items WHERE source_revision_id = ? OR slug IN (SELECT slug FROM service_items WHERE source_revision_id = ?)",
            (revision.get("supersedes_id"), revision.get("supersedes_id")),
        ).fetchone()
        if not item:
            raise ValueError("publication_binding_not_found")
        item_id = item["id"]
        previous_bindings = _bindings(db, current["id"])
        previous_bindings[item_id] = revision_id
        publication_id = f"pub-demo-{uuid.uuid4().hex[:12]}"
        updated = db.execute(
            "UPDATE source_revisions SET status = 'approved', freshness_state = 'pending_review', version = version + 1 "
            "WHERE id = ? AND version = ?",
            (revision_id, expected_version),
        )
        if updated.rowcount != 1:
            raise ValueError("source_revision_version_conflict")
        db.execute(
            """INSERT INTO publications
            (id, label, app_version, retrieval_version, answer_contract_version, status, created_at, previous_id,
             candidate_revision_id, source_bindings, knowledge_model_version, source_gate_result, quality_gate_result,
             responsibility_gate_result, published_at, rollback_target)
            VALUES (?, ?, ?, ?, ?, 'candidate', ?, ?, ?, ?, ?, 'pending', 'pending', 'pending', NULL, NULL)""",
            (
                publication_id,
                f"庆事通演示发布组合 · {revision['title']}",
                current["app_version"],
                current["retrieval_version"],
                current["answer_contract_version"],
                now,
                current["id"],
                revision_id,
                json_dumps(previous_bindings),
                current["knowledge_model_version"],
            ),
        )
        _record_status_transition(
            db,
            publication_id,
            None,
            "candidate",
            now,
            revision_id=revision_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
        db.execute("UPDATE publications SET status = 'building' WHERE id = ?", (publication_id,))
        _record_status_transition(
            db,
            publication_id,
            "candidate",
            "building",
            now,
            revision_id=revision_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
        for bound_item_id, bound_revision_id in previous_bindings.items():
            db.execute(
                "INSERT INTO publication_bindings (publication_id, item_id, revision_id) VALUES (?, ?, ?)",
                (publication_id, bound_item_id, bound_revision_id),
            )
        db.execute("SAVEPOINT publication_build")
        try:
            # Build while the publication is non-public. A failure can never
            # expose partial chunks or indexes to the student retrieval path.
            build_keyword_index(publication_id, connection=db)
            build_selected_dense_index_if_available(publication_id, connection=db)
            conflicts = detect_claim_conflicts(publication_id, include_candidate=True, connection=db)
            if conflicts["conflicts"]:
                raise ValueError("publication_claim_conflict")
        except Exception as error:
            db.execute("ROLLBACK TO publication_build")
            db.execute("RELEASE publication_build")
            db.execute(
                "UPDATE publications SET status = 'checks_failed', source_gate_result = 'pass', "
                "quality_gate_result = 'fail', responsibility_gate_result = 'pending' WHERE id = ?",
                (publication_id,),
            )
            _record_status_transition(
                db,
                publication_id,
                "building",
                "checks_failed",
                now,
                error_code="publication_build_failed",
                error_type=type(error).__name__,
                revision_id=revision_id,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
            )
            result = {
                "status": "checks_failed",
                "code": "publication_build_failed",
                "publication_id": publication_id,
                "previous_publication_id": current["id"],
                "revision_id": revision_id,
                "version": 1,
                "revision_version": expected_version + 1,
            }
            _save_idempotent_result(db, operation, idempotency_key, result)
            return result
        else:
            db.execute("RELEASE publication_build")

        db.execute("UPDATE publications SET status = 'awaiting_business_review' WHERE id = ?", (publication_id,))
        _record_status_transition(
            db,
            publication_id,
            "building",
            "awaiting_business_review",
            now,
            revision_id=revision_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
        result = {
            "revision_id": revision_id,
            "status": "awaiting_business_review",
            "publication_status": "awaiting_business_review",
            "publication_id": publication_id,
            "previous_publication_id": current["id"],
            "review_note": review_note,
            "version": 1,
            "publication_version": 1,
            "revision_version": expected_version + 1,
        }
        _save_idempotent_result(db, operation, idempotency_key, result)
    return result


def complete_business_review(
    publication_id: str,
    decision: str,
    review_note: str,
    expected_version: int,
    idempotency_key: str,
) -> dict[str, Any]:
    """Record the business decision without activating the Publication."""
    now = now_iso()
    review_note = redact_text(review_note)
    with connect() as db:
        _begin_write(db)
        operation = f"publication.business_review:{publication_id}"
        cached = _get_idempotent_result(db, operation, idempotency_key)
        if cached:
            return cached
        publication = db.execute("SELECT * FROM publications WHERE id = ?", (publication_id,)).fetchone()
        if not publication:
            return {"status": "not_found", "publication_id": publication_id}
        if publication["version"] != expected_version:
            raise ValueError("publication_version_conflict")
        if publication["status"] != "awaiting_business_review":
            raise ValueError("business_review_not_allowed")
        revision_id = publication["candidate_revision_id"]
        if decision == "reject":
            updated = db.execute(
                "UPDATE publications SET status = 'checks_failed', source_gate_result = 'fail', "
                "quality_gate_result = 'pass', responsibility_gate_result = 'pending', version = version + 1 "
                "WHERE id = ? AND version = ?",
                (publication_id, expected_version),
            )
            if updated.rowcount != 1:
                raise ValueError("publication_version_conflict")
            db.execute(
                "UPDATE source_revisions SET status = 'rejected', freshness_state = 'possibly_stale', version = version + 1 WHERE id = ?",
                (revision_id,),
            )
            _record_status_transition(
                db,
                publication_id,
                "awaiting_business_review",
                "checks_failed",
                now,
                review_note=review_note,
                error_code="business_review_rejected",
                expected_version=expected_version,
                idempotency_key=idempotency_key,
            )
            db.execute(
                "INSERT INTO audit_events VALUES (?, ?, 'source_revision.rejected', 'source_revision', ?, ?, ?)",
                (new_id("audit"), "business_knowledge_reviewer", revision_id, json_dumps(redact_value({"review_note": review_note, "expected_version": expected_version, "idempotency_key": idempotency_key})), now),
            )
            result = {
                "publication_id": publication_id,
                "revision_id": revision_id,
                "status": "rejected",
                "publication_status": "checks_failed",
                "review_note": review_note,
                "version": expected_version + 1,
            }
            _save_idempotent_result(db, operation, idempotency_key, result)
            return result

        if decision != "approve":
            raise ValueError("business_review_decision_not_allowed")
        updated = db.execute(
            "UPDATE publications SET status = 'ready', source_gate_result = 'pass', "
            "quality_gate_result = 'pass', responsibility_gate_result = 'pending', version = version + 1 "
            "WHERE id = ? AND version = ?",
            (publication_id, expected_version),
        )
        if updated.rowcount != 1:
            raise ValueError("publication_version_conflict")
        _record_status_transition(
            db,
            publication_id,
            "awaiting_business_review",
            "ready",
            now,
            review_note=review_note,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
        db.execute(
            "INSERT INTO audit_events VALUES (?, ?, 'publication.business_reviewed', 'publication', ?, ?, ?)",
            (new_id("audit"), "business_knowledge_reviewer", publication_id, json_dumps(redact_value({"revision_id": revision_id, "review_note": review_note, "expected_version": expected_version, "idempotency_key": idempotency_key})), now),
        )
        result = {
            "publication_id": publication_id,
            "revision_id": revision_id,
            "status": "ready",
            "publication_status": "ready",
            "review_note": review_note,
            "version": expected_version + 1,
        }
        _save_idempotent_result(db, operation, idempotency_key, result)
    return result


def activate_publication(
    publication_id: str,
    release_note: str,
    expected_version: int,
    idempotency_key: str,
) -> dict[str, Any]:
    """Switch a fully gated Publication as the active bundle."""
    now = now_iso()
    release_note = redact_text(release_note)
    operation = f"publication.release:{publication_id}"
    cached = get_idempotent_result(operation, idempotency_key)
    if cached:
        return cached
    with connect() as db:
        _begin_write(db)
        cached = _get_idempotent_result(db, operation, idempotency_key)
        if cached:
            return cached
        retrieval_health = get_retrieval_health(publication_id)
        if retrieval_health.get("keyword_index", {}).get("status") != "ready":
            raise ValueError("publication_index_not_ready")
        publication = db.execute("SELECT * FROM publications WHERE id = ?", (publication_id,)).fetchone()
        current = _current(db)
        if not publication:
            return {"status": "not_found", "publication_id": publication_id}
        if publication["version"] != expected_version:
            raise ValueError("publication_version_conflict")
        if publication["status"] != "ready":
            raise ValueError("publication_activation_not_allowed")
        if not current or current["id"] == publication_id:
            raise ValueError("publication_not_found")
        bindings = _bindings(db, publication_id)
        if not bindings:
            raise ValueError("publication_binding_not_found")
        revision_id = publication["candidate_revision_id"]
        revision = db.execute("SELECT * FROM source_revisions WHERE id = ?", (revision_id,)).fetchone()
        if not revision:
            raise ValueError("source_revision_not_found")

        # The release request is the release executor's responsibility decision.
        # Persist it first, then read the stored row back before changing the
        # active pointer so this path cannot bypass the GateDecision contract.
        from .evaluation import record_release_gate_decision

        recorded_gate = record_release_gate_decision(publication_id, db)
        gate = db.execute(
            "SELECT * FROM gate_decisions WHERE id = ? AND candidate_publication_id = ?",
            (recorded_gate["id"] if recorded_gate else "", publication_id),
        ).fetchone()
        if not gate:
            raise ValueError("publication_gate_not_released")
        gate = dict(gate)
        if (
            gate["decision"] != "release"
            or gate["decided_by"] != "release_executor"
            or gate["fact_source_gate"] != "pass"
            or gate["quality_gate"] != "pass"
            or gate["responsibility_gate"] != "pass"
            or gate["p0_open_count"] != 0
            or gate["p1_open_count"] != 0
        ):
            raise ValueError("publication_gate_not_released")

        db.execute(
            "UPDATE source_revisions SET status = 'published', freshness_state = 'verified_current', version = version + 1 WHERE id = ?",
            (revision_id,),
        )
        if revision["supersedes_id"]:
            db.execute(
                "UPDATE source_revisions SET status = 'superseded', version = version + 1 WHERE id = ? AND id != ?",
                (revision["supersedes_id"], revision_id),
            )
        previous_bindings = _bindings(db, current["id"])
        current_updated = db.execute(
            "UPDATE publications SET status = 'superseded', version = version + 1 WHERE id = ? AND version = ?",
            (current["id"], current["version"]),
        )
        if current_updated.rowcount != 1:
            raise ValueError("publication_version_conflict")
        set_knowledge_publication_status(current["id"], "superseded", connection=db)
        for bound_item_id, bound_revision_id in bindings.items():
            db.execute(
                "UPDATE service_items SET source_revision_id = ? WHERE id = ?",
                (bound_revision_id, bound_item_id),
            )
        from .simulation import mark_tasks_for_source_change

        for bound_item_id, bound_revision_id in bindings.items():
            previous_revision_id = previous_bindings.get(bound_item_id)
            if not previous_revision_id or previous_revision_id == bound_revision_id:
                continue
            impact_rows = db.execute(
                "SELECT id FROM impact_events WHERE source_revision_id = ?",
                (bound_revision_id,),
            ).fetchall()
            for impact_row in impact_rows:
                affected_tasks = mark_tasks_for_source_change(previous_revision_id, impact_row["id"], connection=db)
                db.execute(
                    "UPDATE impact_events SET affected_tasks = ?, status = 'published' WHERE id = ?",
                    (json_dumps(affected_tasks), impact_row["id"]),
                )
        db.execute(
            "UPDATE impact_events SET status = 'published' WHERE source_revision_id = ? AND status != 'published'",
            (revision_id,),
        )
        db.execute(
            "UPDATE publications SET status = 'active', responsibility_gate_result = ?, published_at = ?, version = version + 1 WHERE id = ? AND version = ?",
            (gate["responsibility_gate"], now, publication_id, expected_version),
        )
        if db.execute("SELECT changes()").fetchone()[0] != 1:
            raise ValueError("publication_version_conflict")
        set_knowledge_publication_status(publication_id, "active", connection=db)
        _record_status_transition(
            db,
            publication_id,
            "ready",
            "active",
            now,
            actor_role="release_executor",
            release_note=release_note,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
        db.execute(
            "INSERT INTO audit_events VALUES (?, ?, 'publication.released', 'publication', ?, ?, ?)",
            (new_id("audit"), "release_executor", publication_id, json_dumps(redact_value({"revision_id": revision_id, "previous_id": current["id"], "release_note": release_note, "expected_version": expected_version, "idempotency_key": idempotency_key, "gate_decision_id": gate["id"]})), now),
        )
        result = {
            "revision_id": revision_id,
            "status": "published",
            "publication_status": "active",
            "publication_id": publication_id,
            "previous_publication_id": current["id"],
            "release_note": release_note,
            "gate_decision_id": gate["id"],
            "version": expected_version + 1,
        }
        _save_idempotent_result(db, operation, idempotency_key, result)
    return result


def rollback_publication(
    publication_id: str,
    reason: str,
    expected_version: int,
    idempotency_key: str,
) -> dict[str, Any]:
    now = now_iso()
    reason = redact_text(reason)
    with connect() as db:
        _begin_write(db)
        operation = f"publication.rollback:{publication_id}"
        cached = _get_idempotent_result(db, operation, idempotency_key)
        if cached:
            return cached
        requested = db.execute("SELECT * FROM publications WHERE id = ?", (publication_id,)).fetchone()
        current = _current(db)
        if not requested:
            return {"status": "not_found", "publication_id": publication_id}
        if not current:
            raise ValueError("publication_not_found")
        if current["version"] != expected_version:
            raise ValueError("publication_version_conflict")

        # The primary UI action is "roll back the current release", so a
        # current publication id resolves to its previous complete bundle.
        # Passing an older publication id remains supported for an explicit
        # rollback target.
        if current["id"] == publication_id:
            rollback_target_id = current["previous_id"]
            if not rollback_target_id:
                raise ValueError("rollback_target_not_available")
            target = db.execute("SELECT * FROM publications WHERE id = ?", (rollback_target_id,)).fetchone()
        else:
            rollback_target_id = publication_id
            target = requested
        if not target:
            raise ValueError("rollback_target_not_found")
        if current["id"] == publication_id:
            if target["id"] == current["id"]:
                raise ValueError("rollback_target_is_current")
        if target["status"] not in {"superseded", "published", "active"}:
            raise ValueError("rollback_target_not_verified")
        bindings = _bindings(db, target["id"])
        if not bindings:
            bindings = json_loads(target["source_bindings"], {})
        if not bindings:
            raise ValueError("publication_binding_not_found")

        current_bindings = _bindings(db, current["id"])
        if not current_bindings:
            current_bindings = json_loads(current["source_bindings"], {})
        if current_bindings and set(current_bindings) != set(bindings):
            raise ValueError("publication_binding_set_mismatch")

        current_updated = db.execute(
            "UPDATE publications SET status = 'superseded', rollback_target = ?, version = version + 1 WHERE id = ? AND version = ?",
            (target["id"], current["id"], expected_version),
        )
        if current_updated.rowcount != 1:
            raise ValueError("publication_version_conflict")
        set_knowledge_publication_status(current["id"], "superseded", connection=db)
        _record_status_transition(
            db,
            current["id"],
            current["status"],
            "superseded",
            now,
            actor_role="release_executor",
            rollback_target=target["id"],
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
        target_updated = db.execute(
            "UPDATE publications SET status = 'active', rollback_target = NULL, published_at = ?, version = version + 1 WHERE id = ? AND version = ?",
            (now, target["id"], target["version"]),
        )
        if target_updated.rowcount != 1:
            raise ValueError("publication_version_conflict")
        set_knowledge_publication_status(target["id"], "active", connection=db)
        _record_status_transition(
            db,
            target["id"],
            target["status"],
            "active",
            now,
            actor_role="release_executor",
            rollback_from=current["id"],
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
        for revision_id in set(current_bindings.values()) - set(bindings.values()):
            db.execute(
                "UPDATE source_revisions SET status = 'superseded', freshness_state = 'possibly_stale', version = version + 1 WHERE id = ?",
                (revision_id,),
            )
        for item_id, revision_id in bindings.items():
            db.execute("UPDATE service_items SET source_revision_id = ? WHERE id = ?", (revision_id, item_id))
            db.execute("UPDATE source_revisions SET status = 'published', freshness_state = 'verified_current', version = version + 1 WHERE id = ?", (revision_id,))
        db.execute(
            "INSERT INTO audit_events VALUES (?, ?, 'publication.rolled_back', 'publication', ?, ?, ?)",
            (new_id("audit"), "release_executor", target["id"], json_dumps(redact_value({"from": current["id"], "requested": publication_id, "reason": reason, "expected_version": expected_version, "idempotency_key": idempotency_key})), now),
        )
        build_keyword_index(target["id"], connection=db)
        build_selected_dense_index_if_available(target["id"], connection=db)
        result = {
            "publication_id": target["id"],
            "status": "published",
            "rolled_back_from": current["id"],
            "reason": reason,
            "version": target["version"] + 1,
        }
        _save_idempotent_result(db, operation, idempotency_key, result)
    return result


def get_idempotent_result(operation: str, idempotency_key: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute(
            "SELECT result FROM idempotency_records WHERE operation = ? AND idempotency_key = ?",
            (operation, idempotency_key),
        ).fetchone()
    return json_loads(row["result"], None) if row else None


def save_idempotent_result(operation: str, idempotency_key: str, result: dict[str, Any]) -> None:
    with connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO idempotency_records (operation, idempotency_key, result, created_at) VALUES (?, ?, ?, ?)",
            (operation, idempotency_key, json_dumps(redact_value(result)), now_iso()),
        )
