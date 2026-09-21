from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.getenv("QST_DB_PATH", str(ROOT / "data" / "qingshitong.db")))


def _ensure_parent() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    _ensure_parent()
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def init_db() -> None:
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS publications (
                id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                app_version TEXT NOT NULL,
                retrieval_version TEXT NOT NULL,
                answer_contract_version TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                previous_id TEXT,
                candidate_revision_id TEXT,
                source_bindings TEXT NOT NULL DEFAULT '{}',
                knowledge_model_version TEXT NOT NULL DEFAULT 'knowledge-model-v1',
                source_gate_result TEXT NOT NULL DEFAULT 'pass',
                quality_gate_result TEXT NOT NULL DEFAULT 'pass',
                responsibility_gate_result TEXT NOT NULL DEFAULT 'pass',
                published_at TEXT,
                rollback_target TEXT,
                version INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS source_revisions (
                id TEXT PRIMARY KEY,
                source_key TEXT NOT NULL,
                title TEXT NOT NULL,
                publisher TEXT NOT NULL,
                authority_type TEXT NOT NULL,
                url TEXT NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                published_at TEXT,
                retrieved_at TEXT NOT NULL,
                effective_from TEXT,
                effective_to TEXT,
                status TEXT NOT NULL,
                freshness_state TEXT NOT NULL,
                supersedes_id TEXT,
                created_at TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS service_items (
                id TEXT PRIMARY KEY,
                slug TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                domain TEXT NOT NULL,
                summary TEXT NOT NULL,
                audience TEXT NOT NULL,
                responsible_party TEXT NOT NULL,
                entry_label TEXT NOT NULL,
                entry_url TEXT NOT NULL,
                icon TEXT NOT NULL,
                risk_class TEXT NOT NULL,
                source_revision_id TEXT NOT NULL,
                required_fields TEXT NOT NULL,
                materials TEXT NOT NULL,
                steps TEXT NOT NULL,
                time_windows TEXT NOT NULL,
                aliases TEXT NOT NULL,
                status TEXT NOT NULL,
                FOREIGN KEY(source_revision_id) REFERENCES source_revisions(id)
            );

            CREATE TABLE IF NOT EXISTS scenarios (
                id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL,
                version TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                fields TEXT NOT NULL,
                rules TEXT NOT NULL,
                materials TEXT NOT NULL,
                steps TEXT NOT NULL,
                status TEXT NOT NULL,
                UNIQUE(item_id, version),
                FOREIGN KEY(item_id) REFERENCES service_items(id)
            );

            CREATE TABLE IF NOT EXISTS simulation_tasks (
                id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                actor_type TEXT NOT NULL,
                data_mode TEXT NOT NULL,
                status TEXT NOT NULL,
                form_data TEXT NOT NULL,
                missing_fields TEXT NOT NULL,
                rule_results TEXT NOT NULL,
                material_results TEXT NOT NULL,
                publication_id TEXT NOT NULL,
                source_snapshot_id TEXT NOT NULL,
                submission_snapshot TEXT,
                impact_flags TEXT NOT NULL,
                idempotency_key TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(scenario_id) REFERENCES scenarios(id),
                FOREIGN KEY(publication_id) REFERENCES publications(id)
            );

            CREATE TABLE IF NOT EXISTS simulation_task_submissions (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                submission_version INTEGER NOT NULL,
                task_version INTEGER NOT NULL,
                status TEXT NOT NULL,
                form_data TEXT NOT NULL,
                rule_results TEXT NOT NULL,
                material_results TEXT NOT NULL,
                publication_id TEXT NOT NULL,
                source_snapshot_id TEXT NOT NULL,
                review_note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(task_id, submission_version),
                FOREIGN KEY(task_id) REFERENCES simulation_tasks(id),
                FOREIGN KEY(publication_id) REFERENCES publications(id)
            );

            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                feedback_ref TEXT,
                session_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                input_text TEXT NOT NULL,
                status TEXT NOT NULL,
                intent TEXT NOT NULL,
                context_manifest TEXT NOT NULL,
                steps TEXT NOT NULL,
                response TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                error_code TEXT
            );

            CREATE TABLE IF NOT EXISTS run_steps (
                step_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES runs(id)
            );

            CREATE TABLE IF NOT EXISTS feedback (
                id TEXT PRIMARY KEY,
                response_id TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                rating TEXT NOT NULL,
                reason TEXT,
                comment TEXT,
                created_at TEXT NOT NULL,
                bad_case_id TEXT
            );

            CREATE TABLE IF NOT EXISTS bad_cases (
                id TEXT PRIMARY KEY,
                origin TEXT NOT NULL,
                title TEXT NOT NULL,
                input_text TEXT NOT NULL,
                trace_id TEXT,
                category TEXT NOT NULL,
                severity TEXT NOT NULL,
                expected TEXT NOT NULL,
                actual TEXT NOT NULL,
                status TEXT NOT NULL,
                repair_note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                publication_id TEXT,
                regression_run_id TEXT,
                regression_result TEXT,
                close_note TEXT,
                candidate_fix_type TEXT,
                candidate_fix_ref TEXT,
                sanitized_input TEXT,
                owner_role TEXT,
                version INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS evaluation_runs (
                id TEXT PRIMARY KEY,
                dataset_version TEXT NOT NULL,
                publication_id TEXT NOT NULL,
                retrieval_version TEXT NOT NULL,
                answer_contract_version TEXT NOT NULL,
                app_version TEXT NOT NULL,
                model_profile TEXT NOT NULL,
                runtime_profile TEXT NOT NULL,
                environment TEXT NOT NULL,
                status TEXT NOT NULL,
                summary TEXT NOT NULL,
                cases TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                FOREIGN KEY(publication_id) REFERENCES publications(id)
            );

            CREATE TABLE IF NOT EXISTS evaluation_datasets (
                id TEXT NOT NULL,
                version TEXT NOT NULL,
                name TEXT NOT NULL,
                purpose TEXT NOT NULL,
                case_refs TEXT NOT NULL,
                publication_id TEXT,
                answer_contract_version TEXT,
                status TEXT NOT NULL,
                created_by TEXT NOT NULL,
                approved_by TEXT,
                created_at TEXT NOT NULL,
                approved_at TEXT,
                retired_at TEXT,
                change_note TEXT NOT NULL,
                source_refs TEXT NOT NULL,
                definition_hash TEXT NOT NULL,
                PRIMARY KEY (id, version),
                FOREIGN KEY(publication_id) REFERENCES publications(id)
            );

            CREATE TABLE IF NOT EXISTS evaluation_cases (
                id TEXT NOT NULL,
                version TEXT NOT NULL,
                split TEXT NOT NULL,
                input_text TEXT NOT NULL,
                definition TEXT NOT NULL,
                source_refs TEXT NOT NULL,
                status TEXT NOT NULL,
                definition_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (id, version)
            );

            CREATE TABLE IF NOT EXISTS evaluation_results (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                case_id TEXT NOT NULL,
                case_version TEXT NOT NULL,
                trace_id TEXT,
                status TEXT NOT NULL,
                split TEXT NOT NULL,
                expected TEXT NOT NULL,
                actual TEXT NOT NULL,
                layer_results TEXT NOT NULL,
                program_assertions TEXT NOT NULL,
                judge_result_ids TEXT NOT NULL,
                attempt_ids TEXT NOT NULL,
                human_review_ids TEXT NOT NULL,
                failure_categories TEXT NOT NULL,
                bad_case_id TEXT,
                duration_ms INTEGER,
                error_code TEXT,
                token_estimate INTEGER,
                created_at TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                UNIQUE(run_id, case_id),
                FOREIGN KEY(run_id) REFERENCES evaluation_runs(id)
            );

            CREATE TABLE IF NOT EXISTS evaluation_attempts (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                case_id TEXT NOT NULL,
                case_version TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                status TEXT NOT NULL,
                trace_id TEXT,
                error_code TEXT,
                retryable INTEGER NOT NULL DEFAULT 0,
                payload TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                UNIQUE(run_id, case_id, attempt_no),
                FOREIGN KEY(run_id) REFERENCES evaluation_runs(id)
            );

            CREATE TABLE IF NOT EXISTS evaluation_judge_results (
                id TEXT PRIMARY KEY,
                result_id TEXT NOT NULL,
                judge_profile TEXT NOT NULL,
                judge_version TEXT NOT NULL,
                status TEXT NOT NULL,
                score REAL,
                reason TEXT NOT NULL,
                evidence_refs TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(result_id) REFERENCES evaluation_results(id)
            );

            CREATE TABLE IF NOT EXISTS evaluation_human_reviews (
                id TEXT PRIMARY KEY,
                result_id TEXT NOT NULL,
                review_scope TEXT NOT NULL,
                reviewer_role TEXT NOT NULL,
                decision TEXT NOT NULL,
                note TEXT NOT NULL,
                judge_agreement TEXT NOT NULL DEFAULT 'not_run',
                source_check TEXT NOT NULL DEFAULT 'not_applicable',
                created_at TEXT NOT NULL,
                FOREIGN KEY(result_id) REFERENCES evaluation_results(id)
            );

            CREATE TABLE IF NOT EXISTS evaluation_gate_decisions (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                quality_gate TEXT NOT NULL,
                reason TEXT NOT NULL,
                decided_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES evaluation_runs(id)
            );

            CREATE TABLE IF NOT EXISTS impact_events (
                id TEXT PRIMARY KEY,
                source_revision_id TEXT NOT NULL,
                change_categories TEXT NOT NULL,
                severity TEXT NOT NULL,
                affected_items TEXT NOT NULL,
                affected_tasks TEXT NOT NULL,
                affected_cases TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(source_revision_id) REFERENCES source_revisions(id)
            );

            CREATE TABLE IF NOT EXISTS gate_decisions (
                id TEXT PRIMARY KEY,
                candidate_publication_id TEXT NOT NULL,
                evaluation_run_id TEXT,
                fact_source_gate TEXT NOT NULL,
                quality_gate TEXT NOT NULL,
                validator_gate TEXT NOT NULL DEFAULT 'not_run',
                judge_gate TEXT NOT NULL DEFAULT 'not_run',
                human_review_gate TEXT NOT NULL DEFAULT 'not_run',
                responsibility_gate TEXT NOT NULL,
                p0_open_count INTEGER NOT NULL,
                p1_open_count INTEGER NOT NULL,
                holdout_summary TEXT NOT NULL,
                decision TEXT NOT NULL,
                decided_by TEXT NOT NULL,
                decision_reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(candidate_publication_id) REFERENCES publications(id)
            );

            CREATE TABLE IF NOT EXISTS audit_events (
                id TEXT PRIMARY KEY,
                actor_type TEXT NOT NULL,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                metadata TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS governance_sessions (
                id TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                csrf_token_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                last_used_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS publication_bindings (
                publication_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                PRIMARY KEY (publication_id, item_id),
                FOREIGN KEY(publication_id) REFERENCES publications(id),
                FOREIGN KEY(item_id) REFERENCES service_items(id),
                FOREIGN KEY(revision_id) REFERENCES source_revisions(id)
            );

            CREATE TABLE IF NOT EXISTS knowledge_documents (
                document_id TEXT PRIMARY KEY,
                revision_id TEXT NOT NULL,
                file_type TEXT NOT NULL,
                parse_status TEXT NOT NULL,
                title TEXT NOT NULL,
                structure_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                parser_version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(revision_id, parser_version),
                FOREIGN KEY(revision_id) REFERENCES source_revisions(id)
            );

            CREATE TABLE IF NOT EXISTS evidence_chunks (
                chunk_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                publication_id TEXT NOT NULL,
                service_item_id TEXT,
                title TEXT NOT NULL,
                chunk_text TEXT NOT NULL,
                body TEXT NOT NULL,
                heading_path TEXT NOT NULL,
                block_type TEXT NOT NULL,
                locator TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                support_claims TEXT NOT NULL DEFAULT '[]',
                metadata TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(document_id) REFERENCES knowledge_documents(document_id),
                FOREIGN KEY(revision_id) REFERENCES source_revisions(id),
                FOREIGN KEY(publication_id) REFERENCES publications(id),
                FOREIGN KEY(service_item_id) REFERENCES service_items(id)
            );

            CREATE TABLE IF NOT EXISTS knowledge_claims (
                claim_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                publication_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                value_json TEXT NOT NULL,
                conditions_json TEXT NOT NULL,
                effective_window TEXT,
                audience TEXT NOT NULL,
                source_revision_refs TEXT NOT NULL,
                claim_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(document_id) REFERENCES knowledge_documents(document_id),
                FOREIGN KEY(publication_id) REFERENCES publications(id)
            );

            CREATE TABLE IF NOT EXISTS knowledge_claim_evidence (
                claim_id TEXT NOT NULL,
                chunk_id TEXT NOT NULL,
                publication_id TEXT NOT NULL,
                PRIMARY KEY(claim_id, chunk_id),
                FOREIGN KEY(claim_id) REFERENCES knowledge_claims(claim_id),
                FOREIGN KEY(chunk_id) REFERENCES evidence_chunks(chunk_id),
                FOREIGN KEY(publication_id) REFERENCES publications(id)
            );

            CREATE TABLE IF NOT EXISTS relation_edges (
                edge_id TEXT PRIMARY KEY,
                edge_type TEXT NOT NULL,
                from_id TEXT NOT NULL,
                to_id TEXT NOT NULL,
                publication_id TEXT NOT NULL,
                source_revision_refs TEXT NOT NULL,
                confidence TEXT NOT NULL,
                valid_from TEXT,
                valid_to TEXT,
                status TEXT NOT NULL,
                UNIQUE(publication_id, edge_type, from_id, to_id),
                FOREIGN KEY(publication_id) REFERENCES publications(id)
            );

            CREATE TABLE IF NOT EXISTS chunk_embeddings (
                chunk_id TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                model_revision TEXT NOT NULL,
                dimension INTEGER NOT NULL,
                vector_dtype TEXT NOT NULL,
                normalized INTEGER NOT NULL,
                vector_blob BLOB NOT NULL,
                vector_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(chunk_id, profile_id),
                FOREIGN KEY(chunk_id) REFERENCES evidence_chunks(chunk_id)
            );

            CREATE TABLE IF NOT EXISTS knowledge_indexes (
                index_id TEXT PRIMARY KEY,
                publication_id TEXT NOT NULL,
                index_type TEXT NOT NULL,
                profile_id TEXT,
                keyword_analyzer TEXT NOT NULL,
                relation_schema TEXT NOT NULL,
                build_status TEXT NOT NULL,
                build_hash TEXT NOT NULL,
                model_revision TEXT,
                dimension INTEGER,
                normalized INTEGER,
                created_at TEXT NOT NULL,
                UNIQUE(publication_id, index_type, profile_id),
                FOREIGN KEY(publication_id) REFERENCES publications(id)
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS evidence_chunks_fts USING fts5(
                title,
                body,
                content='evidence_chunks',
                content_rowid='rowid',
                tokenize='unicode61 remove_diacritics 2'
            );

            CREATE TRIGGER IF NOT EXISTS evidence_chunks_ai AFTER INSERT ON evidence_chunks BEGIN
                INSERT INTO evidence_chunks_fts(rowid, title, body)
                VALUES (new.rowid, new.title, new.body);
            END;

            CREATE TRIGGER IF NOT EXISTS evidence_chunks_ad AFTER DELETE ON evidence_chunks BEGIN
                INSERT INTO evidence_chunks_fts(evidence_chunks_fts, rowid, title, body)
                VALUES ('delete', old.rowid, old.title, old.body);
            END;

            CREATE TRIGGER IF NOT EXISTS evidence_chunks_au AFTER UPDATE ON evidence_chunks BEGIN
                INSERT INTO evidence_chunks_fts(evidence_chunks_fts, rowid, title, body)
                VALUES ('delete', old.rowid, old.title, old.body);
                INSERT INTO evidence_chunks_fts(rowid, title, body)
                VALUES (new.rowid, new.title, new.body);
            END;

            CREATE TABLE IF NOT EXISTS idempotency_records (
                operation TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                result TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(operation, idempotency_key)
            );

            CREATE TABLE IF NOT EXISTS workstudy_jobs (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                department TEXT NOT NULL,
                location TEXT NOT NULL,
                schedule TEXT NOT NULL,
                stipend TEXT NOT NULL,
                qualification TEXT NOT NULL,
                deadline TEXT NOT NULL,
                status TEXT NOT NULL,
                created_by TEXT NOT NULL,
                internal_note TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_service_items_domain ON service_items(domain);
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON simulation_tasks(status);
            CREATE INDEX IF NOT EXISTS idx_task_submissions_task ON simulation_task_submissions(task_id, submission_version);
            CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at);
            CREATE INDEX IF NOT EXISTS idx_run_steps_run_id ON run_steps(run_id, step_index);
            CREATE INDEX IF NOT EXISTS idx_bad_cases_status ON bad_cases(status);
            CREATE INDEX IF NOT EXISTS idx_source_status ON source_revisions(status, freshness_state);
            CREATE INDEX IF NOT EXISTS idx_workstudy_jobs_status ON workstudy_jobs(status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_publication_bindings_revision ON publication_bindings(revision_id);
            CREATE INDEX IF NOT EXISTS idx_knowledge_documents_revision ON knowledge_documents(revision_id);
            CREATE INDEX IF NOT EXISTS idx_evidence_chunks_publication ON evidence_chunks(publication_id, service_item_id);
            CREATE INDEX IF NOT EXISTS idx_evidence_chunks_revision ON evidence_chunks(revision_id);
            CREATE INDEX IF NOT EXISTS idx_knowledge_claims_publication ON knowledge_claims(publication_id, subject, predicate, claim_status);
            CREATE INDEX IF NOT EXISTS idx_claim_evidence_chunk ON knowledge_claim_evidence(chunk_id, claim_id);
            CREATE INDEX IF NOT EXISTS idx_relation_edges_publication ON relation_edges(publication_id, edge_type, status);
            CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_profile ON chunk_embeddings(profile_id, chunk_id);
            CREATE INDEX IF NOT EXISTS idx_knowledge_indexes_publication ON knowledge_indexes(publication_id, index_type, build_status);
            CREATE INDEX IF NOT EXISTS idx_governance_sessions_expiry ON governance_sessions(expires_at, revoked_at);
            CREATE INDEX IF NOT EXISTS idx_evaluation_results_run ON evaluation_results(run_id, case_id);
            CREATE INDEX IF NOT EXISTS idx_evaluation_attempts_run ON evaluation_attempts(run_id, case_id, attempt_no);
            CREATE INDEX IF NOT EXISTS idx_evaluation_judge_results_result ON evaluation_judge_results(result_id);
            CREATE INDEX IF NOT EXISTS idx_evaluation_human_reviews_result ON evaluation_human_reviews(result_id);
            """
        )
        _ensure_column(db, "publications", "candidate_revision_id", "TEXT" )
        _ensure_column(db, "publications", "source_bindings", "TEXT NOT NULL DEFAULT '{}'" )
        _ensure_column(db, "publications", "knowledge_model_version", "TEXT NOT NULL DEFAULT 'knowledge-model-v1'" )
        _ensure_column(db, "publications", "source_gate_result", "TEXT NOT NULL DEFAULT 'pass'" )
        _ensure_column(db, "publications", "quality_gate_result", "TEXT NOT NULL DEFAULT 'pass'" )
        _ensure_column(db, "publications", "responsibility_gate_result", "TEXT NOT NULL DEFAULT 'pass'" )
        _ensure_column(db, "publications", "published_at", "TEXT" )
        _ensure_column(db, "publications", "rollback_target", "TEXT" )
        _ensure_column(db, "publications", "version", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(db, "source_revisions", "version", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(db, "evaluation_results", "version", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(db, "evidence_chunks", "embedding_ref", "TEXT" )
        _ensure_column(db, "evidence_chunks", "keyword_index_ref", "TEXT" )
        _ensure_column(db, "knowledge_indexes", "model_revision", "TEXT" )
        _ensure_column(db, "knowledge_indexes", "dimension", "INTEGER" )
        _ensure_column(db, "knowledge_indexes", "normalized", "INTEGER" )
        _ensure_column(db, "runs", "feedback_ref", "TEXT" )
        db.execute("CREATE INDEX IF NOT EXISTS idx_runs_feedback_ref ON runs(feedback_ref)")
        _ensure_column(db, "bad_cases", "publication_id", "TEXT" )
        _ensure_column(db, "bad_cases", "regression_run_id", "TEXT" )
        _ensure_column(db, "bad_cases", "regression_result", "TEXT" )
        _ensure_column(db, "bad_cases", "close_note", "TEXT" )
        _ensure_column(db, "bad_cases", "candidate_fix_type", "TEXT" )
        _ensure_column(db, "bad_cases", "candidate_fix_ref", "TEXT" )
        _ensure_column(db, "bad_cases", "sanitized_input", "TEXT" )
        _ensure_column(db, "bad_cases", "owner_role", "TEXT" )
        _ensure_column(db, "bad_cases", "version", "INTEGER NOT NULL DEFAULT 1" )
        _ensure_column(db, "evaluation_results", "error_code", "TEXT" )
        _ensure_column(db, "evaluation_results", "token_estimate", "INTEGER" )
        _ensure_column(db, "evaluation_human_reviews", "judge_agreement", "TEXT NOT NULL DEFAULT 'not_run'" )
        _ensure_column(db, "evaluation_human_reviews", "source_check", "TEXT NOT NULL DEFAULT 'not_applicable'" )
        _ensure_column(db, "evaluation_runs", "retrieval_version", "TEXT NOT NULL DEFAULT 'legacy/unknown'" )
        _ensure_column(db, "evaluation_runs", "answer_contract_version", "TEXT NOT NULL DEFAULT 'legacy/unknown'" )
        _ensure_column(db, "evaluation_runs", "app_version", "TEXT NOT NULL DEFAULT 'legacy/unknown'" )
        _ensure_column(db, "evaluation_runs", "model_profile", "TEXT NOT NULL DEFAULT 'legacy/unknown'" )
        _ensure_column(db, "evaluation_runs", "environment", "TEXT NOT NULL DEFAULT 'legacy/unknown'" )
        _ensure_column(db, "evaluation_runs", "dataset_id", "TEXT NOT NULL DEFAULT 'legacy/unknown'" )
        _ensure_column(db, "evaluation_runs", "run_mode", "TEXT NOT NULL DEFAULT 'release'" )
        _ensure_column(db, "evaluation_runs", "embedding_profile", "TEXT NOT NULL DEFAULT 'legacy/unknown'" )
        _ensure_column(db, "evaluation_runs", "judge_profile", "TEXT" )
        _ensure_column(db, "evaluation_runs", "total_cases", "INTEGER NOT NULL DEFAULT 0" )
        _ensure_column(db, "evaluation_runs", "completed_cases", "INTEGER NOT NULL DEFAULT 0" )
        _ensure_column(db, "evaluation_runs", "attempt_count", "INTEGER NOT NULL DEFAULT 0" )
        _ensure_column(db, "evaluation_runs", "parent_run_id", "TEXT" )
        _ensure_column(db, "evaluation_runs", "manifest", "TEXT NOT NULL DEFAULT '{}'" )
        _ensure_column(db, "evaluation_runs", "manifest_hash", "TEXT NOT NULL DEFAULT 'legacy/unknown'" )
        _ensure_column(db, "evaluation_runs", "version", "INTEGER NOT NULL DEFAULT 1" )
        _ensure_column(db, "gate_decisions", "validator_gate", "TEXT NOT NULL DEFAULT 'not_run'" )
        _ensure_column(db, "gate_decisions", "judge_gate", "TEXT NOT NULL DEFAULT 'not_run'" )
        _ensure_column(db, "gate_decisions", "human_review_gate", "TEXT NOT NULL DEFAULT 'not_run'" )
        db.execute("CREATE INDEX IF NOT EXISTS idx_evaluation_runs_mode_status ON evaluation_runs(run_mode, status, started_at)")


def backup_database(destination: str | Path) -> dict[str, Any]:
    """Create and verify a consistent SQLite backup without copying WAL files."""
    source_path = Path(DB_PATH)
    destination_path = Path(destination)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if source_path.resolve() == destination_path.resolve():
        raise ValueError("backup_destination_must_differ")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination_path.with_name(f".{destination_path.name}.tmp")
    if temporary_path.exists():
        temporary_path.unlink()

    try:
        source = sqlite3.connect(source_path)
        target = sqlite3.connect(temporary_path)
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close()
            source.close()

        check = sqlite3.connect(temporary_path)
        try:
            integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise sqlite3.DatabaseError(f"backup_integrity_failed:{integrity}")
            schema_version = check.execute("PRAGMA user_version").fetchone()[0]
            publication = check.execute(
                "SELECT id, app_version, retrieval_version, knowledge_model_version FROM publications "
                "WHERE status IN ('active', 'published') ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        finally:
            check.close()

        digest = hashlib.sha256()
        with temporary_path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        temporary_path.replace(destination_path)
        return {
            "path": str(destination_path),
            "backup_method": "sqlite_connection_backup",
            "integrity_check": integrity,
            "sha256": digest.hexdigest(),
            "schema_version": schema_version,
            "publication_id": publication[0] if publication else None,
            "app_version": publication[1] if publication else None,
            "retrieval_version": publication[2] if publication else None,
            "knowledge_model_version": publication[3] if publication else None,
        }
    except Exception:
        if temporary_path.exists():
            temporary_path.unlink()
        raise


def _ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
