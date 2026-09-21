from __future__ import annotations

import hashlib
import json
import re
from contextlib import nullcontext
from datetime import date, datetime, timezone
from typing import Any, Mapping

from .db import connect, json_dumps, json_loads
from .embedding import (
    EmbeddingProfile,
    EmbeddingUnavailableError,
    embedding_profile_health,
    load_embedding_encoder,
    load_embedding_profile,
)


STOP_WORDS = {"怎么", "应该", "需要", "一下", "我的", "有没有", "可以", "现在", "请问", "想要", "帮我"}
BLOCKED_SOURCE_STATES = {
    "pending_review",
    "possibly_stale",
    "conflicted",
    "temporarily_unavailable",
    "withdrawn",
    "no_evidence",
}
RETRIEVAL_STRATEGIES = {"a", "b", "c"}
DEFAULT_CHANNELS = ("standalone_web", "portal_sim", "wecom_sim")
PUBLICATION_RETRIEVAL_STATUSES = ("active", "published", "superseded")
PRE_RELEASE_RETRIEVAL_STATUSES = (*PUBLICATION_RETRIEVAL_STATUSES, "ready")
CHUNK_TARGET_CHARS = 768
CHUNK_HARD_LIMIT_CHARS = 960
QUERY_REWRITE_ALIASES = {
    "借教室": ("场地", "办活动"),
    "教室": ("场地",),
    "迎新会": ("办活动",),
    "新生见面": ("办活动",),
    "学生兼职": ("勤工助学",),
    "请病休": ("病假",),
    "看病请假": ("病假",),
}


def _tokens(text: str) -> set[str]:
    raw = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_-]{2,}", text.lower())
    return {item for item in raw if item not in STOP_WORDS}


def _row_to_item(row) -> dict[str, Any]:
    item = dict(row)
    for key in ("required_fields", "materials", "steps", "time_windows", "aliases"):
        item[key] = json_loads(item.get(key), [])
    return item


def _publication_bindings(publication_id: str | None, *, allow_pre_release: bool = False) -> dict[str, str]:
    if not publication_id:
        return {}
    allowed_statuses = PRE_RELEASE_RETRIEVAL_STATUSES if allow_pre_release else PUBLICATION_RETRIEVAL_STATUSES
    with connect() as db:
        publication = db.execute("SELECT status FROM publications WHERE id = ?", (publication_id,)).fetchone()
        if not publication or publication["status"] not in allowed_statuses:
            return {}
        rows = db.execute(
            "SELECT item_id, revision_id FROM publication_bindings WHERE publication_id = ? ORDER BY item_id",
            (publication_id,),
        ).fetchall()
    return {row["item_id"]: row["revision_id"] for row in rows}


def list_items(publication_id: str | None = None, *, allow_pre_release: bool = False) -> list[dict[str, Any]]:
    publication_id = publication_id or get_current_publication().get("id")
    bindings = _publication_bindings(publication_id, allow_pre_release=allow_pre_release)
    if not bindings:
        return []
    with connect() as db:
        rows = db.execute("SELECT * FROM service_items WHERE status = 'published' ORDER BY id").fetchall()
    items = []
    for row in rows:
        if row["id"] not in bindings:
            continue
        item = _row_to_item(row)
        item["source_revision_id"] = bindings[row["id"]]
        items.append(item)
    return items


def list_scenarios() -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            """SELECT s.*, i.slug, i.title, i.domain, i.icon, i.summary, i.entry_label
            FROM scenarios s JOIN service_items i ON i.id = s.item_id
            WHERE s.status = 'published' ORDER BY s.id"""
        ).fetchall()
    result = []
    for row in rows:
        value = dict(row)
        for key in ("fields", "rules", "materials", "steps"):
            value[key] = json_loads(value.get(key), [])
        result.append(value)
    return result


def student_business_text(value: Any) -> Any:
    """Remove internal demo qualifiers before exposing business copy to students."""
    if not isinstance(value, str):
        return value
    cleaned = value
    for marker in ("（演示版）", "（演示对象）", "（演示角色）", "（演示提示）", "（虚拟责任方）"):
        cleaned = cleaned.replace(marker, "")
    for marker in ("演示", "模拟", "虚拟责任方", "虚拟申请", "虚拟审核", "虚拟审批"):
        cleaned = cleaned.replace(marker, "")
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = cleaned.replace("（）", "").replace(" · ·", " ·")
    return cleaned.strip()


def student_bootstrap_view() -> dict[str, Any]:
    """Return the minimal bootstrap contract for the student workbench."""
    items = list_items()
    item_ids = {item["id"] for item in items}
    item_views = [
        {
            "id": item["id"],
            "title": student_business_text(item["title"]),
            "domain": student_business_text(item["domain"]),
            "summary": student_business_text(item["summary"]),
            "audience": student_business_text(item["audience"]),
            "responsible_party": student_business_text(item.get("responsible_party")),
            "materials": [student_business_text(value) for value in item["materials"]],
            "steps": [student_business_text(value) for value in item["steps"]],
            "entry_label": student_business_text(item["entry_label"]),
            "entry_url": item.get("entry_url"),
        }
        for item in items
    ]
    scenario_views = []
    for scenario in list_scenarios():
        if scenario.get("item_id") not in item_ids:
            continue
        fields = []
        for field in scenario.get("fields") or []:
            fields.append(
                {
                    "key": field.get("key"),
                    "label": student_business_text(field.get("label", field.get("key"))),
                    "type": field.get("type", "text"),
                    "options": [student_business_text(value) for value in field.get("options", [])],
                    "required": bool(field.get("required")),
                }
            )
        scenario_views.append(
            {
                "id": scenario["id"],
                "item_id": scenario["item_id"],
                "name": student_business_text(scenario["name"]),
                "description": student_business_text(scenario["description"]),
                "fields": fields,
                "steps": [student_business_text(value) for value in scenario.get("steps") or []],
            }
        )
    return {
        "items": item_views,
        "scenarios": scenario_views,
        "disclaimer": "当前页面使用脱敏、改写和虚拟化数据；当前未接入学校真实业务系统。",
    }


def get_scenario(scenario_id: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute(
            """SELECT s.*, i.slug, i.title, i.domain, i.icon, i.summary, i.audience, i.responsible_party,
               i.entry_label, i.entry_url, i.required_fields, i.materials AS item_materials,
               i.steps AS item_steps, i.time_windows, i.source_revision_id, i.risk_class
            FROM scenarios s JOIN service_items i ON i.id = s.item_id WHERE s.id = ?""",
            (scenario_id,),
        ).fetchone()
    if not row:
        return None
    value = dict(row)
    for key in ("fields", "rules", "materials", "steps", "required_fields", "item_materials", "item_steps", "time_windows"):
        value[key] = json_loads(value.get(key), [])
    return value


def get_item_by_slug(
    slug: str,
    publication_id: str | None = None,
    *,
    allow_pre_release: bool = False,
) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in list_items(publication_id, allow_pre_release=allow_pre_release)
            if item["slug"] == slug
        ),
        None,
    )


def get_current_publication() -> dict[str, Any]:
    with connect() as db:
        row = db.execute(
            "SELECT * FROM publications WHERE status IN ('active', 'published') "
            "ORDER BY published_at DESC, created_at DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else {}


def get_source(source_id: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM source_revisions WHERE id = ?", (source_id,)).fetchone()
    return dict(row) if row else None


def list_sources() -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute("SELECT * FROM source_revisions ORDER BY retrieved_at DESC").fetchall()
    return [dict(row) for row in rows]


def rewrite_query(query: str) -> dict[str, Any]:
    added_terms: list[str] = []
    for phrase, replacements in QUERY_REWRITE_ALIASES.items():
        if phrase in query:
            for replacement in replacements:
                if replacement not in query and replacement not in added_terms:
                    added_terms.append(replacement)
    rewritten = " ".join([query, *added_terms]).strip()
    return {"original": query, "rewritten": rewritten, "added_terms": added_terms}


PARSER_VERSION = "source-normalizer-v2"
KEYWORD_ANALYZER = "fts5-unicode61-cjk-bigram-v1"
RELATION_SCHEMA = "service-item-relations-v1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _fts_terms(text: str, vocabulary: set[str] | None = None) -> list[str]:
    """Create deterministic terms; known business phrases take precedence over CJK runs."""
    terms: list[str] = []
    for run in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9][A-Za-z0-9_-]*", text.lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", run):
            matched = sorted(
                (term for term in (vocabulary or set()) if len(term) >= 2 and term in run and term not in STOP_WORDS),
                key=lambda value: (-len(value), value),
            )
            terms.extend(matched or [run])
        elif run not in STOP_WORDS:
            terms.append(run)
    return _unique([term for term in terms if term not in STOP_WORDS])


def _fts_body(parts: list[str], vocabulary: set[str]) -> str:
    return " ".join(_fts_terms(" ".join(parts), vocabulary))


def _fts_query(text: str, vocabulary: set[str]) -> str:
    terms = _fts_terms(text, vocabulary)
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _publication_binding_tokens(db, publication_id: str) -> list[str]:
    rows = db.execute(
        "SELECT item_id, revision_id FROM publication_bindings WHERE publication_id = ? ORDER BY item_id",
        (publication_id,),
    ).fetchall()
    return [f"{row['item_id']}:{row['revision_id']}" for row in rows]


def _keyword_build_hash(publication_id: str, db, chunk_hashes: list[str] | None = None) -> str:
    if chunk_hashes is None:
        rows = db.execute(
            "SELECT chunk_id, content_hash FROM evidence_chunks WHERE publication_id = ? ORDER BY chunk_id",
            (publication_id,),
        ).fetchall()
        chunk_hashes = [f"{row['chunk_id']}:{row['content_hash']}" for row in rows]
    return _content_hash(
        "\n".join(
            [
                publication_id,
                PARSER_VERSION,
                KEYWORD_ANALYZER,
                RELATION_SCHEMA,
                *_publication_binding_tokens(db, publication_id),
                *sorted(chunk_hashes),
            ]
        )
    )


def _dense_build_hash(publication_id: str, profile: EmbeddingProfile, db, chunk_hashes: list[str] | None = None) -> str:
    if chunk_hashes is None:
        rows = db.execute(
            """SELECT ec.chunk_id, ec.content_hash, ce.vector_hash
            FROM evidence_chunks ec
            LEFT JOIN chunk_embeddings ce
              ON ce.chunk_id = ec.chunk_id AND ce.profile_id = ?
            WHERE ec.publication_id = ? ORDER BY ec.chunk_id""",
            (profile.profile_id, publication_id),
        ).fetchall()
        chunk_hashes = [
            f"{row['chunk_id']}:{row['content_hash']}:{row['vector_hash'] or ''}"
            for row in rows
        ]
    return _content_hash(
        "\n".join(
            [
                publication_id,
                KEYWORD_ANALYZER,
                RELATION_SCHEMA,
                profile.profile_id,
                profile.model_revision,
                str(profile.dimension),
                str(profile.normalized),
                *_publication_binding_tokens(db, publication_id),
                *sorted(chunk_hashes),
            ]
        )
    )


def _canonical_text(content: str) -> str:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalized.split("\n")).strip()


def _markdown_structure(content: str) -> dict[str, Any]:
    lines = content.splitlines()
    blocks: list[dict[str, Any]] = []
    heading_stack: list[str] = []
    active_kind: str | None = None
    active_lines: list[str] = []
    active_start = 0
    active_heading_path: list[str] = []

    def flush() -> None:
        nonlocal active_kind, active_lines, active_start, active_heading_path
        if not active_kind or not active_lines:
            active_kind = None
            active_lines = []
            return
        blocks.append(
            {
                "block_type": active_kind,
                "text": "\n".join(active_lines).strip(),
                "heading_path": list(active_heading_path),
                "locator": f"line:{active_start}",
            }
        )
        active_kind = None
        active_lines = []

    for line_number, line in enumerate(lines, start=1):
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if heading:
            flush()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            heading_stack = heading_stack[: level - 1]
            heading_stack.append(title)
            blocks.append(
                {
                    "block_type": "heading",
                    "text": title,
                    "heading_path": list(heading_stack),
                    "locator": f"line:{line_number}",
                }
            )
            continue
        if not line.strip():
            flush()
            continue
        if re.match(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", line):
            kind = "list"
        elif "|" in line and line.count("|") >= 2:
            kind = "table"
        else:
            kind = "paragraph"
        if active_kind != kind or active_heading_path != heading_stack:
            flush()
            active_kind = kind
            active_start = line_number
            active_heading_path = list(heading_stack)
        active_lines.append(line.strip())
    flush()
    return {"kind": "markdown", "blocks": blocks}


def _source_file_type(source: dict[str, Any]) -> tuple[str, str]:
    source_key = str(source.get("source_key", "")).lower().split("?", 1)[0]
    if source_key.endswith((".md", ".markdown")):
        return "md", "file"
    if source_key.endswith(".json"):
        return "json", "file"
    for suffix in (".pdf", ".docx", ".xlsx", ".doc", ".xls"):
        if source_key.endswith(suffix):
            return suffix[1:], "unsupported_file"
    if source_key.startswith("virtual-") or source.get("authority_type") == "virtual_design":
        return "json", "virtual_seed"
    return "unknown", "unknown"


def normalize_revision(revision_id: str, connection=None) -> dict[str, Any]:
    """Turn an approved source snapshot into a deterministic KnowledgeDocument shape."""
    with (connect() if connection is None else nullcontext(connection)) as db:
        row = db.execute("SELECT * FROM source_revisions WHERE id = ?", (revision_id,)).fetchone()
        if not row:
            raise ValueError("source_revision_not_found")
        source = dict(row)

    file_type, source_kind = _source_file_type(source)
    base = {
        "document_id": f"doc-{revision_id}",
        "revision_id": revision_id,
        "file_type": file_type,
        "title": source["title"],
        "parser_version": PARSER_VERSION,
        "source_kind": source_kind,
    }
    if source["status"] not in {"approved", "published", "superseded"}:
        return {
            **base,
            "parse_status": "needs_review",
            "structure": {"kind": "source", "reason": "source_not_published"},
            "normalized_content": "",
            "content_hash": "",
        }
    if source_kind == "unsupported_file" or source_kind == "unknown":
        return {
            **base,
            "parse_status": "needs_review",
            "structure": {"kind": "unsupported", "reason": "r0_file_type_not_supported"},
            "normalized_content": "",
            "content_hash": "",
        }

    raw_content = str(source.get("content") or "")
    if source_kind == "file" and file_type == "json":
        try:
            value = json.loads(raw_content)
        except json.JSONDecodeError:
            return {
                **base,
                "parse_status": "failed",
                "structure": {"kind": "json", "reason": "invalid_json"},
                "normalized_content": "",
                "content_hash": "",
            }
        normalized_content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        structure = {"kind": "json", "value": value}
    elif source_kind == "file" and file_type == "md":
        normalized_content = _canonical_text(raw_content)
        structure = _markdown_structure(normalized_content)
    else:
        normalized_content = _canonical_text(raw_content)
        structure = {"kind": "virtual_seed", "blocks": _markdown_structure(normalized_content)["blocks"]}

    return {
        **base,
        "parse_status": "ready",
        "structure": structure,
        "normalized_content": normalized_content,
        "content_hash": _content_hash(normalized_content),
    }


def _search_vocabulary(items: list[dict[str, Any]]) -> set[str]:
    values: list[str] = list(QUERY_REWRITE_ALIASES) + [term for terms in QUERY_REWRITE_ALIASES.values() for term in terms]
    for item in items:
        values.extend([item["title"], item["domain"], *item["aliases"], *item["materials"], *item["steps"], *item["time_windows"]])
    return {value.lower() for value in values if value and len(value) >= 2}


def _normalized_chunks(normalized: dict[str, Any]) -> list[dict[str, Any]]:
    """Compile parser blocks into bounded chunks without losing local structure."""
    structure = normalized.get("structure") or {}
    blocks = structure.get("blocks") or structure.get("source_structure", {}).get("blocks") or []
    chunks: list[dict[str, Any]] = []
    for block in blocks:
        if block.get("block_type") == "heading":
            continue
        text = str(block.get("text") or "").strip()
        if not text:
            continue
        heading_path = list(block.get("heading_path") or [])
        prefix = " / ".join(heading_path)
        block_type = block.get("block_type", "paragraph")
        units = [unit.strip() for unit in text.splitlines() if unit.strip()] if block_type in {"table", "list"} else [
            unit.strip() for unit in re.split(r"(?<=[。！？；.!?;])\s*|\n+", text) if unit.strip()
        ]
        if not units:
            units = [text]
        if block_type == "table" and len(units) > 1:
            table_header = "\n".join(units[:2]) if units[1].lstrip().startswith("|") and "---" in units[1] else units[0]
        else:
            table_header = ""

        chunk_start = len(chunks)
        body_target_chars = max(1, CHUNK_TARGET_CHARS - (len(prefix) + 1 if prefix else 0))
        body_hard_limit_chars = max(1, CHUNK_HARD_LIMIT_CHARS - (len(prefix) + 1 if prefix else 0))
        part_number = 0
        current = ""

        def emit(value: str, suffix: str | None = None) -> None:
            nonlocal part_number
            value = value.strip()
            if not value:
                return
            part_number += 1
            contextual = f"{prefix}\n{value}" if prefix else value
            chunks.append(
                {
                    "text": contextual,
                    "heading_path": heading_path,
                    "block_type": block_type,
                    "locator": f"{block.get('locator', 'document')}{suffix or ''}",
                }
            )

        for unit in units:
            if len(unit) > body_hard_limit_chars:
                if current:
                    emit(current, f"#part-{part_number + 1}")
                    current = ""
                for start in range(0, len(unit), body_hard_limit_chars):
                    emit(unit[start : start + body_hard_limit_chars], f"#part-{part_number + 1}")
                continue
            candidate = f"{current}\n{unit}" if current else unit
            if current and len(candidate) > body_target_chars:
                emit(current, f"#part-{part_number + 1}")
                current = unit
            else:
                current = candidate
        if current:
            emit(current, f"#part-{part_number + 1}" if part_number else "")

        new_chunks = chunks[chunk_start:]
        if block_type == "table" and table_header and len(new_chunks) > 1:
            for chunk in new_chunks[1:]:
                if table_header not in chunk["text"]:
                    body = chunk["text"]
                    chunk["text"] = f"{prefix}\n{table_header}\n{body}" if prefix else f"{table_header}\n{body}"
    if chunks:
        return chunks
    fallback = str(normalized.get("normalized_content") or "").strip()
    return [
        {
            "text": fallback,
            "heading_path": [],
            "block_type": "paragraph",
            "locator": "document",
        }
    ] if fallback else []


def _knowledge_status_for_publication(publication_status: str) -> str:
    if publication_status in {"active", "published"}:
        return "published"
    if publication_status == "superseded":
        return "superseded"
    return "candidate"


def _persist_knowledge_graph(db, publication_id: str, publication_status: str, created_at: str) -> None:
    """Persist the rebuildable claim/evidence and relation projections for one Publication."""
    db.execute(
        "DELETE FROM knowledge_claim_evidence WHERE publication_id = ?",
        (publication_id,),
    )
    db.execute("DELETE FROM knowledge_claims WHERE publication_id = ?", (publication_id,))
    db.execute("DELETE FROM relation_edges WHERE publication_id = ?", (publication_id,))

    claim_status = _knowledge_status_for_publication(publication_status)
    chunks = db.execute(
        """SELECT ec.chunk_id, ec.document_id, ec.revision_id, ec.service_item_id,
                  ec.chunk_text, ec.locator, i.slug, i.audience, sr.effective_from, sr.effective_to
           FROM evidence_chunks ec
           JOIN service_items i ON i.id = ec.service_item_id
           JOIN source_revisions sr ON sr.id = ec.revision_id
          WHERE ec.publication_id = ? ORDER BY ec.chunk_id""",
        (publication_id,),
    ).fetchall()
    for chunk in chunks:
        claim_id = f"claim:{publication_id}:{chunk['chunk_id']}"
        db.execute(
            """INSERT INTO knowledge_claims
            (claim_id, document_id, publication_id, subject, predicate, value_json, conditions_json,
             effective_window, audience, source_revision_refs, claim_status, created_at)
            VALUES (?, ?, ?, ?, 'source_statement', ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(claim_id) DO UPDATE SET
                document_id = excluded.document_id,
                publication_id = excluded.publication_id,
                value_json = excluded.value_json,
                conditions_json = excluded.conditions_json,
                effective_window = excluded.effective_window,
                audience = excluded.audience,
                source_revision_refs = excluded.source_revision_refs,
                claim_status = excluded.claim_status,
                created_at = excluded.created_at""",
            (
                claim_id,
                chunk["document_id"],
                publication_id,
                f"service_item:{chunk['service_item_id']}",
                json_dumps({"text": chunk["chunk_text"], "locator": chunk["locator"]}),
                json_dumps({"locator": chunk["locator"]}),
                json_dumps({"effective_from": chunk["effective_from"], "effective_to": chunk["effective_to"]}),
                chunk["audience"],
                json_dumps([chunk["revision_id"]]),
                claim_status,
                created_at,
            ),
        )
        db.execute(
            "INSERT INTO knowledge_claim_evidence (claim_id, chunk_id, publication_id) VALUES (?, ?, ?)",
            (claim_id, chunk["chunk_id"], publication_id),
        )
        db.execute(
            "UPDATE evidence_chunks SET support_claims = ? WHERE chunk_id = ?",
            (json_dumps([chunk["slug"], claim_id]), chunk["chunk_id"]),
        )

    items = db.execute(
        """SELECT i.id, i.materials, i.steps, i.time_windows, i.responsible_party,
                  i.source_revision_id, sr.effective_from, sr.effective_to
           FROM service_items i
           JOIN publication_bindings pb ON pb.item_id = i.id AND pb.publication_id = ?
           JOIN source_revisions sr ON sr.id = pb.revision_id
          WHERE i.status = 'published' ORDER BY i.id""",
        (publication_id,),
    ).fetchall()
    for item in items:
        values_by_type = {
            "item_to_material": json_loads(item["materials"], []),
            "item_to_step": json_loads(item["steps"], []),
            "item_to_condition": json_loads(item["time_windows"], []),
            "item_to_party": [item["responsible_party"]],
            "item_to_revision": [item["source_revision_id"]],
        }
        for edge_type, values in values_by_type.items():
            for value in values:
                target = str(value)
                edge_id = f"edge:{publication_id}:{edge_type}:{item['id']}:{hashlib.sha256(target.encode('utf-8')).hexdigest()[:16]}"
                db.execute(
                    """INSERT INTO relation_edges
                    (edge_id, edge_type, from_id, to_id, publication_id, source_revision_refs,
                     confidence, valid_from, valid_to, status)
                    VALUES (?, ?, ?, ?, ?, ?, 'explicit', ?, ?, ?)
                    ON CONFLICT(publication_id, edge_type, from_id, to_id) DO UPDATE SET
                        edge_id = excluded.edge_id,
                        source_revision_refs = excluded.source_revision_refs,
                        valid_from = excluded.valid_from,
                        valid_to = excluded.valid_to,
                        status = excluded.status""",
                    (
                        edge_id,
                        edge_type,
                        item["id"],
                        target,
                        publication_id,
                        json_dumps([item["source_revision_id"]]),
                        item["effective_from"],
                        item["effective_to"],
                        claim_status,
                    ),
                )


def detect_claim_conflicts(
    publication_id: str,
    *,
    include_candidate: bool = False,
    connection=None,
) -> dict[str, Any]:
    """Find conflicting structured claims without selecting a winning value."""
    statuses = ("'candidate'", "'approved'", "'published'") if include_candidate else ("'approved'", "'published'")
    status_sql = ", ".join(statuses)
    with (connect() if connection is None else nullcontext(connection)) as db:
        rows = db.execute(
            f"""SELECT subject, predicate, claim_id, value_json
               FROM knowledge_claims
              WHERE publication_id = ?
                AND claim_status IN ({status_sql})
                AND predicate != 'source_statement'
               ORDER BY subject, predicate, claim_id""",
            (publication_id,),
        ).fetchall()
    grouped: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for row in rows:
        grouped.setdefault((row["subject"], row["predicate"]), []).append((row["claim_id"], row["value_json"]))
    conflicts = [
        {"subject": subject, "predicate": predicate, "claim_ids": [claim_id for claim_id, _ in values]}
        for (subject, predicate), values in grouped.items()
        if len({value for _, value in values}) > 1
    ]
    return {"status": "detected" if conflicts else "none", "conflicts": conflicts}


def set_knowledge_publication_status(publication_id: str, publication_status: str, connection=None) -> None:
    """Keep persisted claims and relation edges aligned with Publication state."""
    status = _knowledge_status_for_publication(publication_status)
    with (connect() if connection is None else nullcontext(connection)) as db:
        db.execute("UPDATE knowledge_claims SET claim_status = ? WHERE publication_id = ?", (status, publication_id))
        db.execute("UPDATE relation_edges SET status = ? WHERE publication_id = ?", (status, publication_id))


def build_keyword_index(publication_id: str, connection=None) -> dict[str, Any]:
    """Compile the published seed snapshot into normalized documents and FTS5."""
    with (connect() if connection is None else nullcontext(connection)) as db:
        publication = db.execute("SELECT id, status FROM publications WHERE id = ?", (publication_id,)).fetchone()
        if not publication or publication["status"] not in {"candidate", "building", "ready", "active", "published", "superseded"}:
            raise ValueError("publication_not_found")
        bindings = db.execute(
            "SELECT item_id, revision_id FROM publication_bindings WHERE publication_id = ? ORDER BY item_id",
            (publication_id,),
        ).fetchall()
        db.execute(
            "DELETE FROM chunk_embeddings WHERE chunk_id IN "
            "(SELECT chunk_id FROM evidence_chunks WHERE publication_id = ?)",
            (publication_id,),
        )
        db.execute("DELETE FROM knowledge_claim_evidence WHERE publication_id = ?", (publication_id,))
        db.execute("DELETE FROM knowledge_claims WHERE publication_id = ?", (publication_id,))
        db.execute("DELETE FROM relation_edges WHERE publication_id = ?", (publication_id,))
        db.execute("DELETE FROM evidence_chunks WHERE publication_id = ?", (publication_id,))
        db.execute(
            "DELETE FROM knowledge_indexes WHERE publication_id = ? AND index_type IN ('keyword', 'dense')",
            (publication_id,),
        )
        items = {
            row["id"]: _row_to_item(row)
            for row in db.execute("SELECT * FROM service_items WHERE status = 'published' ORDER BY id").fetchall()
        }
        vocabulary = _search_vocabulary(list(items.values()))
        chunk_hashes: list[str] = []
        created_at = _now_iso()
        for binding in bindings:
            item = items.get(binding["item_id"])
            source = db.execute("SELECT * FROM source_revisions WHERE id = ?", (binding["revision_id"],)).fetchone()
            if not item or not source:
                continue
            normalized = normalize_revision(source["id"], connection=db)
            if normalized["parse_status"] != "ready":
                continue
            document_id = normalized["document_id"]
            structure = {
                "kind": "service_item_snapshot",
                "source_structure": normalized["structure"],
                "service_item_id": item["id"],
                "publication_id": publication_id,
                "source_revision_id": source["id"],
            }
            db.execute(
                """INSERT INTO knowledge_documents
                (document_id, revision_id, file_type, parse_status, title, structure_json, content_hash, parser_version, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    revision_id = excluded.revision_id,
                    file_type = excluded.file_type,
                    parse_status = excluded.parse_status,
                    title = excluded.title,
                    structure_json = excluded.structure_json,
                    content_hash = excluded.content_hash,
                    parser_version = excluded.parser_version,
                    created_at = excluded.created_at""",
                (document_id, source["id"], normalized["file_type"], normalized["parse_status"], normalized["title"], json_dumps(structure), normalized["content_hash"], normalized["parser_version"], created_at),
            )
            chunks = _normalized_chunks(normalized)
            for chunk_number, chunk in enumerate(chunks, start=1):
                chunk_text = chunk["text"]
                search_parts = [
                    source["title"],
                    item["title"],
                    item["summary"],
                    item["domain"],
                    item["audience"],
                    *item["aliases"],
                    *item["materials"],
                    *item["steps"],
                    *item["time_windows"],
                    chunk_text,
                ]
                chunk_hash = _content_hash(chunk_text)
                chunk_id = f"{publication_id}:{source['id']}:{chunk_number:04d}"
                metadata = {
                    "authority_type": source["authority_type"],
                    "publisher_label": source["publisher"],
                    "visibility_scope": "public_demo",
                    "risk_class": item["risk_class"],
                    "freshness_state": source["freshness_state"],
                    "effective_from": source["effective_from"],
                    "effective_to": source["effective_to"],
                    "simulation": True,
                    "channels": list(DEFAULT_CHANNELS),
                    "language": "zh-CN",
                }
                db.execute(
                    """INSERT INTO evidence_chunks
                    (chunk_id, document_id, revision_id, publication_id, service_item_id, title, chunk_text, body,
                     heading_path, block_type, locator, content_hash, support_claims, metadata, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chunk_id) DO UPDATE SET
                        document_id = excluded.document_id,
                        revision_id = excluded.revision_id,
                        publication_id = excluded.publication_id,
                        service_item_id = excluded.service_item_id,
                        title = excluded.title,
                        chunk_text = excluded.chunk_text,
                        body = excluded.body,
                        heading_path = excluded.heading_path,
                        block_type = excluded.block_type,
                        locator = excluded.locator,
                        content_hash = excluded.content_hash,
                        support_claims = excluded.support_claims,
                        metadata = excluded.metadata,
                        created_at = excluded.created_at""",
                    (
                        chunk_id,
                        document_id,
                        source["id"],
                        publication_id,
                        item["id"],
                        item["title"],
                        chunk_text,
                        _fts_body(search_parts, vocabulary),
                        json_dumps(chunk["heading_path"]),
                        chunk["block_type"],
                        chunk["locator"],
                        chunk_hash,
                        json_dumps([item["slug"]]),
                        json_dumps(metadata),
                        created_at,
                    ),
                )
                db.execute(
                    "UPDATE evidence_chunks SET keyword_index_ref = ? WHERE chunk_id = ?",
                    (f"keyword:{publication_id}", chunk_id),
                )
                chunk_hashes.append(f"{chunk_id}:{chunk_hash}")

        _persist_knowledge_graph(db, publication_id, publication["status"], created_at)
        build_hash = _keyword_build_hash(publication_id, db, chunk_hashes)
        db.execute("INSERT INTO evidence_chunks_fts(evidence_chunks_fts) VALUES ('rebuild')")
        db.execute(
            """INSERT INTO knowledge_indexes
            (index_id, publication_id, index_type, profile_id, keyword_analyzer, relation_schema, build_status, build_hash, created_at)
            VALUES (?, ?, 'keyword', 'keyword-only', ?, ?, 'ready', ?, ?)
            ON CONFLICT(publication_id, index_type, profile_id) DO UPDATE SET
                keyword_analyzer = excluded.keyword_analyzer,
                relation_schema = excluded.relation_schema,
                build_status = excluded.build_status,
                build_hash = excluded.build_hash,
                created_at = excluded.created_at""",
            (f"idx-{publication_id}-keyword", publication_id, KEYWORD_ANALYZER, RELATION_SCHEMA, build_hash, created_at),
        )
    return {"publication_id": publication_id, "index_type": "keyword", "build_status": "ready", "build_hash": build_hash, "chunk_count": len(chunk_hashes)}


def build_dense_index(
    publication_id: str,
    profile: EmbeddingProfile | None = None,
    encoder=None,
    connection=None,
) -> dict[str, Any]:
    """Build the selected Profile's exact SQLite Dense index for one Publication."""
    profile = profile or load_embedding_profile()
    encoder = encoder or load_embedding_encoder()
    encoder_profile = getattr(encoder, "profile", None)
    if encoder_profile and encoder_profile.profile_id != profile.profile_id:
        raise EmbeddingUnavailableError("embedding_profile_mismatch")
    try:
        import numpy as np
    except ImportError as error:  # pragma: no cover - dependency is declared for runtime use
        raise EmbeddingUnavailableError("runtime_dependency_missing:numpy") from error

    with (connect() if connection is None else nullcontext(connection)) as db:
        publication = db.execute("SELECT id, status FROM publications WHERE id = ?", (publication_id,)).fetchone()
        if not publication or publication["status"] not in {"candidate", "building", "ready", "active", "published", "superseded"}:
            raise ValueError("publication_not_found")
        chunks = db.execute(
            "SELECT chunk_id, chunk_text, content_hash FROM evidence_chunks WHERE publication_id = ? ORDER BY chunk_id",
            (publication_id,),
        ).fetchall()
        texts = [row["chunk_text"] for row in chunks]
        try:
            vectors = encoder.encode_documents(texts) if texts else np.empty((0, profile.dimension), dtype="<f4")
        except EmbeddingUnavailableError:
            raise
        except Exception as error:
            raise EmbeddingUnavailableError("embedding_encode_failed") from error
        vectors = np.asarray(vectors, dtype="<f4")
        if vectors.ndim == 1 and len(chunks) == 1:
            vectors = vectors.reshape(1, -1)
        if vectors.shape != (len(chunks), profile.dimension):
            raise EmbeddingUnavailableError("embedding_dimension_mismatch")
        if not np.isfinite(vectors).all():
            raise EmbeddingUnavailableError("embedding_non_finite")
        if profile.normalized and len(vectors):
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            if (norms == 0).any():
                raise EmbeddingUnavailableError("embedding_zero_vector")
            vectors = vectors / norms
        vectors = vectors.astype("<f4", copy=False)

        chunk_hashes: list[str] = []
        now = _now_iso()
        for row, vector in zip(chunks, vectors, strict=True):
            vector_blob = vector.tobytes()
            vector_hash = hashlib.sha256(vector_blob).hexdigest()
            db.execute(
                """INSERT INTO chunk_embeddings
                (chunk_id, profile_id, model_revision, dimension, vector_dtype, normalized,
                 vector_blob, vector_hash, created_at)
                VALUES (?, ?, ?, ?, 'float32', ?, ?, ?, ?)
                ON CONFLICT(chunk_id, profile_id) DO UPDATE SET
                    model_revision = excluded.model_revision,
                    dimension = excluded.dimension,
                    vector_dtype = excluded.vector_dtype,
                    normalized = excluded.normalized,
                    vector_blob = excluded.vector_blob,
                    vector_hash = excluded.vector_hash,
                    created_at = excluded.created_at""",
                (
                    row["chunk_id"],
                    profile.profile_id,
                    profile.model_revision,
                    profile.dimension,
                    int(profile.normalized),
                    vector_blob,
                    vector_hash,
                    now,
                ),
            )
            db.execute(
                "UPDATE evidence_chunks SET embedding_ref = ? WHERE chunk_id = ?",
                (f"embedding:{profile.profile_id}:{row['chunk_id']}", row["chunk_id"]),
            )
            chunk_hashes.append(f"{row['chunk_id']}:{row['content_hash']}:{vector_hash}")

        build_hash = _dense_build_hash(publication_id, profile, db, chunk_hashes)
        db.execute(
            """INSERT INTO knowledge_indexes
            (index_id, publication_id, index_type, profile_id, keyword_analyzer, relation_schema,
             build_status, build_hash, model_revision, dimension, normalized, created_at)
            VALUES (?, ?, 'dense', ?, ?, ?, 'ready', ?, ?, ?, ?, ?)
            ON CONFLICT(publication_id, index_type, profile_id) DO UPDATE SET
                keyword_analyzer = excluded.keyword_analyzer,
                relation_schema = excluded.relation_schema,
                build_status = excluded.build_status,
                build_hash = excluded.build_hash,
                model_revision = excluded.model_revision,
                dimension = excluded.dimension,
                normalized = excluded.normalized,
                created_at = excluded.created_at""",
            (
                f"idx-{publication_id}-dense-{profile.profile_id}",
                publication_id,
                profile.profile_id,
                KEYWORD_ANALYZER,
                RELATION_SCHEMA,
                build_hash,
                profile.model_revision,
                profile.dimension,
                int(profile.normalized),
                now,
            ),
        )
    return {
        "publication_id": publication_id,
        "index_type": "dense",
        "profile_id": profile.profile_id,
        "build_status": "ready",
        "build_hash": build_hash,
        "chunk_count": len(chunks),
    }


def build_selected_dense_index_if_available(publication_id: str, connection=None) -> dict[str, Any]:
    """Build the configured Profile, while preserving the explicit sparse fallback."""
    profile = load_embedding_profile()
    health = embedding_profile_health()
    if health["status"] != "ready":
        return {
            "publication_id": publication_id,
            "index_type": "dense",
            "profile_id": profile.profile_id,
            "build_status": "unavailable",
            "reason": health["reason"],
        }
    return build_dense_index(publication_id, profile=profile, connection=connection)


def get_retrieval_health(publication_id: str | None = None) -> dict[str, Any]:
    publication_id = publication_id or get_current_publication().get("id")
    dense_profile = embedding_profile_health()
    if not publication_id:
        return {
            "status": "unavailable",
            "publication_id": None,
            "keyword_index": {"status": "missing", "index_id": None, "build_hash": None},
            "dense_profile": dense_profile,
            "dense_index": {"status": "missing", "index_id": None, "build_hash": None},
        }
    selected_profile = load_embedding_profile()
    with connect() as db:
        publication = db.execute("SELECT id, status FROM publications WHERE id = ?", (publication_id,)).fetchone()
        keyword_index = db.execute(
            """SELECT index_id, build_status, build_hash, profile_id, keyword_analyzer, relation_schema
            FROM knowledge_indexes WHERE publication_id = ? AND index_type = 'keyword'
            ORDER BY created_at DESC LIMIT 1""",
            (publication_id,),
        ).fetchone()
        dense_index = db.execute(
            """SELECT index_id, build_status, build_hash, profile_id, model_revision, dimension, normalized,
                      keyword_analyzer, relation_schema
            FROM knowledge_indexes
            WHERE publication_id = ? AND index_type = 'dense' AND profile_id = ?
            ORDER BY created_at DESC LIMIT 1""",
            (publication_id, dense_profile["profile_id"]),
        ).fetchone()
        keyword_status = keyword_index["build_status"] if keyword_index else "missing"
        if keyword_index and any(
            keyword_index[key] != value
            for key, value in {
                "keyword_analyzer": KEYWORD_ANALYZER,
                "relation_schema": RELATION_SCHEMA,
            }.items()
        ):
            keyword_status = "incompatible"
        if keyword_index and keyword_status == "ready":
            expected_keyword_hash = _keyword_build_hash(publication_id, db)
            if keyword_index["build_hash"] != expected_keyword_hash:
                keyword_status = "incompatible"
        expected_dense = {
            "profile_id": dense_profile["profile_id"],
            "model_revision": dense_profile["model_revision"],
            "dimension": dense_profile["dimension"],
            "normalized": int(bool(dense_profile["normalized"])),
            "keyword_analyzer": KEYWORD_ANALYZER,
            "relation_schema": RELATION_SCHEMA,
        }
        dense_status = dense_index["build_status"] if dense_index else "missing"
        if dense_index and any(dense_index[key] != value for key, value in expected_dense.items()):
            dense_status = "incompatible"
        if dense_index and dense_status == "ready":
            expected_dense_hash = _dense_build_hash(publication_id, selected_profile, db)
            if dense_index["build_hash"] != expected_dense_hash:
                dense_status = "incompatible"
        if not publication or publication["status"] not in {"ready", "active", "published", "superseded"}:
            return {
            "status": "unavailable",
            "publication_id": publication_id,
            "keyword_index": {"status": "publication_unavailable", "index_id": None, "build_hash": None},
            "dense_profile": dense_profile,
            "dense_index": {"status": "publication_unavailable", "index_id": None, "build_hash": None},
        }
    return {
        "status": "ready" if keyword_status == "ready" and dense_profile["status"] == "ready" and dense_status == "ready" else ("degraded" if keyword_status == "ready" else "unavailable"),
        "publication_id": publication_id,
        "keyword_index": {
            "status": keyword_status,
            "index_id": keyword_index["index_id"] if keyword_index else None,
            "build_hash": keyword_index["build_hash"] if keyword_index else None,
            "profile_id": keyword_index["profile_id"] if keyword_index else None,
        },
        "dense_profile": {**dense_profile, "reason": "profile_not_built" if dense_profile["status"] == "not_ready" else dense_profile["reason"]},
        "dense_index": {
            "status": dense_status,
            "index_id": dense_index["index_id"] if dense_index else None,
            "build_hash": dense_index["build_hash"] if dense_index else None,
            "profile_id": dense_index["profile_id"] if dense_index else dense_profile["profile_id"],
            "model_revision": dense_index["model_revision"] if dense_index else None,
            "dimension": dense_index["dimension"] if dense_index else None,
            "normalized": bool(dense_index["normalized"]) if dense_index else None,
        },
    }


def _relation_edges(item: dict[str, Any]) -> list[dict[str, str]]:
    return [
        *[{"relation": "has_material", "target": value} for value in item["materials"]],
        *[{"relation": "has_step", "target": value} for value in item["steps"]],
        *[{"relation": "has_time_window", "target": value} for value in item["time_windows"]],
        {"relation": "responsible_party", "target": item["responsible_party"]},
    ]


def _evidence_record(
    item: dict[str, Any],
    source: dict[str, Any],
    publication_id: str | None,
    reference_date: date | None = None,
) -> dict[str, Any]:
    corpus = " ".join(
        [item["title"], item["summary"], item["domain"], item["audience"], *item["aliases"], *item["materials"], *item["steps"], *item["time_windows"]]
    ).lower()
    return {
        "evidence_id": f"evidence-{item['source_revision_id']}-{item['id']}",
        "service_item_id": item["id"],
        "slug": item["slug"],
        "title": item["title"],
        "source_revision_id": item["source_revision_id"],
        "source_title": source.get("title", "演示来源"),
        "source_url": source.get("url", ""),
        "publication_id": publication_id,
        "revision_id": item["source_revision_id"],
        "chunk_id": f"{item['source_revision_id']}:summary",
        "locator": f"虚拟来源正文：{source.get('title', '演示来源')} / 事项摘要",
        "support_claims": [item["slug"]],
        "content_hash": source.get("content_hash", ""),
        "published_at": source.get("published_at"),
        "freshness_state": source.get("freshness_state", "no_evidence"),
        "retrieved_at": source.get("retrieved_at"),
        "authority_type": source.get("authority_type", "virtual_design"),
        "citation_allowed": _citation_allowed(source, reference_date),
        "_corpus": corpus,
        "_item": item,
    }


def _evidence_record_from_chunk(
    row,
    item: dict[str, Any],
    source: dict[str, Any],
    publication_id: str,
    reference_date: date | None = None,
    allow_pre_release: bool = False,
) -> dict[str, Any]:
    metadata = json_loads(row["metadata"], {})
    return {
        "evidence_id": f"evidence-{row['chunk_id']}",
        "service_item_id": item["id"],
        "slug": item["slug"],
        "title": item["title"],
        "source_revision_id": row["revision_id"],
        "source_title": source.get("title", "演示来源"),
        "source_url": source.get("url", ""),
        "publication_id": publication_id,
        "revision_id": row["revision_id"],
        "chunk_id": row["chunk_id"],
        "locator": row["locator"],
        "support_claims": json_loads(row["support_claims"], [item["slug"]]),
        "content_hash": row["content_hash"],
        "published_at": source.get("published_at"),
        "freshness_state": metadata.get("freshness_state", source.get("freshness_state", "no_evidence")),
        "retrieved_at": source.get("retrieved_at"),
        "authority_type": metadata.get("authority_type", source.get("authority_type", "virtual_design")),
        "citation_allowed": _citation_allowed(source, reference_date, allow_pre_release=allow_pre_release),
    }


def _normalize_retrieval_filters(filters: Mapping[str, Any] | None) -> dict[str, Any]:
    if not filters:
        return {}
    normalized: dict[str, Any] = {}
    for key in ("service_item_id", "domain", "channel"):
        value = filters.get(key)
        if isinstance(value, str) and value.strip():
            normalized[key] = value.strip()
    audience = filters.get("audience")
    if isinstance(audience, str) and audience.strip():
        normalized["audience"] = [audience.strip()]
    elif isinstance(audience, (list, tuple, set)):
        normalized["audience"] = [str(value).strip() for value in audience if str(value).strip()]
    if "simulation" in filters and filters["simulation"] is not None:
        normalized["simulation"] = bool(filters["simulation"])
    confirmed_slots = filters.get("confirmed_slots")
    if isinstance(confirmed_slots, (list, tuple, set)):
        normalized["confirmed_slots"] = [str(value).strip() for value in confirmed_slots if str(value).strip()]
    return normalized


def _passes_retrieval_filters(
    item: dict[str, Any],
    metadata: dict[str, Any],
    filters: dict[str, Any],
    filter_stats: dict[str, Any] | None,
) -> bool:
    reasons: list[str] = []
    if filters.get("service_item_id") and item["id"] != filters["service_item_id"]:
        reasons.append("service_item_mismatch")
    if filters.get("domain") and item["domain"] != filters["domain"]:
        reasons.append("domain_mismatch")
    requested_audiences = filters.get("audience") or []
    if requested_audiences and not any(value in item["audience"] for value in requested_audiences):
        reasons.append("audience_mismatch")
    if filters.get("simulation") is not None and bool(metadata.get("simulation")) != filters["simulation"]:
        reasons.append("simulation_boundary_mismatch")
    requested_channel = filters.get("channel")
    allowed_channels = set(metadata.get("channels") or DEFAULT_CHANNELS)
    if requested_channel and requested_channel not in allowed_channels:
        reasons.append("channel_boundary_mismatch")
    requested_slots = set(filters.get("confirmed_slots") or [])
    if requested_slots:
        available_slots = {field.get("key") for field in item.get("required_fields", []) if isinstance(field, dict)}
        if not requested_slots.issubset(available_slots):
            reasons.append("confirmed_slot_not_supported")
    if reasons and filter_stats is not None:
        rejections = filter_stats.setdefault("rejections", {})
        for reason in reasons:
            rejections[reason] = rejections.get(reason, 0) + 1
    return not reasons


def _citation_allowed(
    source: dict[str, Any],
    reference_date: date | None = None,
    *,
    allow_pre_release: bool = False,
) -> bool:
    """Allow public sources, or candidate sources only inside evaluation scope."""
    status = source.get("status") or source.get("source_status")
    freshness_state = source.get("freshness_state", "no_evidence")
    allowed_statuses = {"published", "superseded", "approved"} if allow_pre_release else {"published", "superseded"}
    blocked_states = BLOCKED_SOURCE_STATES - {"pending_review"} if allow_pre_release else BLOCKED_SOURCE_STATES
    return (
        status in allowed_statuses
        and freshness_state not in blocked_states
        and _effective_window_allows(source, reference_date)
    )


def _effective_window_allows(source: dict[str, Any], reference_date: date | None = None) -> bool:
    today = reference_date or datetime.now(timezone.utc).date()
    for field, is_lower_bound in (("effective_from", True), ("effective_to", False)):
        value = source.get(field)
        if not value:
            continue
        try:
            boundary = datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
        except ValueError:
            return False
        if is_lower_bound and today < boundary:
            return False
        if not is_lower_bound and today > boundary:
            return False
    return True


def _fts_candidates(
    query: str,
    publication_id: str,
    limit: int,
    reference_date: date,
    filters: Mapping[str, Any] | None = None,
    filter_stats: dict[str, Any] | None = None,
    allow_pre_release: bool = False,
) -> list[dict[str, Any]]:
    normalized_filters = _normalize_retrieval_filters(filters)
    publication_statuses = PRE_RELEASE_RETRIEVAL_STATUSES if allow_pre_release else PUBLICATION_RETRIEVAL_STATUSES
    publication_status_placeholders = ", ".join("?" for _ in publication_statuses)
    source_statuses = ("published", "superseded", "approved") if allow_pre_release else ("published", "superseded")
    source_status_placeholders = ", ".join("?" for _ in source_statuses)
    blocked_source_states = BLOCKED_SOURCE_STATES - {"pending_review"} if allow_pre_release else BLOCKED_SOURCE_STATES
    blocked_state_placeholders = ", ".join("?" for _ in blocked_source_states)
    with connect() as db:
        items = {
            row["id"]: _row_to_item(row)
            for row in db.execute("SELECT * FROM service_items WHERE status = 'published' ORDER BY id").fetchall()
        }
        match_query = _fts_query(query, _search_vocabulary(list(items.values())))
        if not match_query:
            return []
        rows = db.execute(
            f"""SELECT ec.*, bm25(evidence_chunks_fts, 10.0, 1.0) AS fts_rank
            FROM evidence_chunks_fts
            JOIN evidence_chunks ec ON ec.rowid = evidence_chunks_fts.rowid
            JOIN publication_bindings pb
              ON pb.publication_id = ec.publication_id
             AND pb.item_id = ec.service_item_id
             AND pb.revision_id = ec.revision_id
            JOIN publications p ON p.id = ec.publication_id
            JOIN source_revisions sr ON sr.id = ec.revision_id
              WHERE evidence_chunks_fts MATCH ?
               AND ec.publication_id = ?
               AND p.status IN ({publication_status_placeholders})
                AND sr.status IN ({source_status_placeholders})
               AND json_extract(ec.metadata, '$.visibility_scope') = 'public_demo'
              AND sr.freshness_state NOT IN ({blocked_state_placeholders})
              AND (sr.effective_from IS NULL OR sr.effective_from <= ?)
              AND (sr.effective_to IS NULL OR sr.effective_to >= ?)
             ORDER BY fts_rank ASC, ec.chunk_id ASC""",
             (match_query, publication_id, *publication_statuses, *source_statuses, *blocked_source_states, reference_date.isoformat(), reference_date.isoformat()),
        ).fetchall()
        sources = {row["id"]: dict(row) for row in db.execute("SELECT * FROM source_revisions").fetchall()}
    candidates: list[dict[str, Any]] = []
    for rank, row in enumerate(rows, start=1):
        item = items.get(row["service_item_id"])
        source = sources.get(row["revision_id"], {})
        if not item or not source:
            continue
        metadata = json_loads(row["metadata"], {})
        if not _passes_retrieval_filters(item, metadata, normalized_filters, filter_stats):
            continue
        record = _evidence_record_from_chunk(row, item, source, publication_id, reference_date, allow_pre_release)
        record["sparse_score"] = round(max(0.0, -float(row["fts_rank"])), 6)
        record["matched_terms"] = []
        record["relation_edges"] = _relation_edges(item)
        record["relation_score"] = 0.0
        record["rrf_score"] = 1.0 / (60 + rank)
        record["rerank_score"] = record["sparse_score"]
        record["score"] = record["sparse_score"]
        record["retrieved_rank"] = rank
        candidates.append(record)
    return candidates[:limit]


def _dense_candidates(
    query: str,
    publication_id: str,
    limit: int,
    reference_date: date,
    filters: Mapping[str, Any] | None = None,
    filter_stats: dict[str, Any] | None = None,
    allow_pre_release: bool = False,
) -> list[dict[str, Any]]:
    normalized_filters = _normalize_retrieval_filters(filters)
    publication_statuses = PRE_RELEASE_RETRIEVAL_STATUSES if allow_pre_release else PUBLICATION_RETRIEVAL_STATUSES
    publication_status_placeholders = ", ".join("?" for _ in publication_statuses)
    source_statuses = ("published", "superseded", "approved") if allow_pre_release else ("published", "superseded")
    source_status_placeholders = ", ".join("?" for _ in source_statuses)
    blocked_source_states = BLOCKED_SOURCE_STATES - {"pending_review"} if allow_pre_release else BLOCKED_SOURCE_STATES
    blocked_state_placeholders = ", ".join("?" for _ in blocked_source_states)
    profile = load_embedding_profile()
    encoder = load_embedding_encoder()
    try:
        import numpy as np
    except ImportError as error:  # pragma: no cover - dependency is declared for runtime use
        raise EmbeddingUnavailableError("runtime_dependency_missing:numpy") from error

    with connect() as db:
        index = db.execute(
            """SELECT build_status, model_revision, dimension, normalized
            FROM knowledge_indexes
            WHERE publication_id = ? AND index_type = 'dense' AND profile_id = ?
            ORDER BY created_at DESC LIMIT 1""",
            (publication_id, profile.profile_id),
        ).fetchone()
        if not index or index["build_status"] != "ready":
            return []
        if (
            index["model_revision"] != profile.model_revision
            or index["dimension"] != profile.dimension
            or bool(index["normalized"]) != profile.normalized
        ):
            return []
        rows = db.execute(
            f"""SELECT ec.*, ce.vector_blob, sr.title AS source_title, sr.url AS source_url,
                      sr.content_hash AS source_content_hash, sr.published_at, sr.retrieved_at,
                      sr.authority_type, sr.freshness_state, sr.status AS source_status,
                      i.slug, i.title AS item_title, i.summary, i.domain, i.audience,
                      i.responsible_party, i.required_fields, i.materials, i.steps,
                      i.time_windows, i.aliases, i.risk_class, i.entry_label, i.entry_url,
                      i.icon, i.source_revision_id AS item_source_revision_id, i.status AS item_status
               FROM chunk_embeddings ce
               JOIN evidence_chunks ec ON ec.chunk_id = ce.chunk_id
               JOIN publication_bindings pb
                 ON pb.publication_id = ec.publication_id
                AND pb.item_id = ec.service_item_id
                AND pb.revision_id = ec.revision_id
               JOIN publications p ON p.id = ec.publication_id
               JOIN source_revisions sr ON sr.id = ec.revision_id
               JOIN service_items i ON i.id = ec.service_item_id
              WHERE ce.profile_id = ?
                 AND ec.publication_id = ?
                 AND p.status IN ({publication_status_placeholders})
                 AND sr.status IN ({source_status_placeholders})
                 AND json_extract(ec.metadata, '$.visibility_scope') = 'public_demo'
                 AND sr.freshness_state NOT IN ({blocked_state_placeholders})
                AND (sr.effective_from IS NULL OR sr.effective_from <= ?)
                AND (sr.effective_to IS NULL OR sr.effective_to >= ?)""",
             (profile.profile_id, publication_id, *publication_statuses, *source_statuses, *blocked_source_states, reference_date.isoformat(), reference_date.isoformat()),
        ).fetchall()

    query_vector = np.asarray(encoder.encode_query(query), dtype="<f4")
    if query_vector.ndim == 2:
        query_vector = query_vector[0]
    if query_vector.shape != (profile.dimension,):
        raise EmbeddingUnavailableError("embedding_dimension_mismatch")
    if profile.normalized:
        query_norm = np.linalg.norm(query_vector)
        if query_norm == 0:
            raise EmbeddingUnavailableError("embedding_zero_vector")
        query_vector = query_vector / query_norm

    candidates: list[dict[str, Any]] = []
    for row in rows:
        vector = np.frombuffer(row["vector_blob"], dtype="<f4")
        if vector.shape != (profile.dimension,):
            raise EmbeddingUnavailableError("embedding_dimension_mismatch")
        score = float(np.dot(query_vector, vector))
        item = {
            "id": row["service_item_id"],
            "slug": row["slug"],
            "title": row["item_title"],
            "summary": row["summary"],
            "domain": row["domain"],
            "audience": row["audience"],
            "responsible_party": row["responsible_party"],
            "required_fields": json_loads(row["required_fields"], []),
            "materials": json_loads(row["materials"], []),
            "steps": json_loads(row["steps"], []),
            "time_windows": json_loads(row["time_windows"], []),
            "aliases": json_loads(row["aliases"], []),
            "risk_class": row["risk_class"],
        }
        metadata = json_loads(row["metadata"], {})
        if not _passes_retrieval_filters(item, metadata, normalized_filters, filter_stats):
            continue
        source = {
            "title": row["source_title"],
            "url": row["source_url"],
            "content_hash": row["source_content_hash"],
            "published_at": row["published_at"],
            "retrieved_at": row["retrieved_at"],
            "authority_type": row["authority_type"],
            "freshness_state": row["freshness_state"],
            "source_status": row["source_status"],
        }
        record = _evidence_record_from_chunk(row, item, source, publication_id, reference_date, allow_pre_release)
        record["dense_score"] = round(score, 6)
        record["sparse_score"] = 0.0
        record["matched_terms"] = []
        record["relation_edges"] = _relation_edges(item)
        record["relation_score"] = 0.0
        record["rrf_score"] = 0.0
        record["score"] = score
        candidates.append(record)
    candidates.sort(key=lambda item: (-item["dense_score"], item["chunk_id"]))
    for rank, record in enumerate(candidates[:limit], start=1):
        record["dense_rank"] = rank
    return candidates[:limit]


def _sparse_score(record: dict[str, Any], query: str) -> tuple[float, list[str]]:
    query_tokens = _tokens(query)
    matched = sorted(token for token in query_tokens if token in record["_corpus"])
    exact_bonus = sum(3 for alias in record["_item"]["aliases"] if alias.lower() in query.lower())
    return float(len(matched) + exact_bonus), matched


def _rrf_scores(rankings: list[list[str]], constant: int = 60) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item_id in enumerate(ranking, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (constant + rank)
    return scores


def validate_claim_evidence(claims: list[Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    claim_specs: list[dict[str, Any]] = []
    for claim in claims:
        if isinstance(claim, dict):
            label = str(claim.get("claim_id") or claim.get("text") or "").strip()
            refs = [str(ref).strip() for ref in claim.get("evidence_refs", []) if str(ref).strip()]
        else:
            label = str(claim).strip()
            refs = [label] if label else []
        if label:
            claim_specs.append({"label": label, "refs": refs})

    claim_labels = [spec["label"] for spec in claim_specs]
    claim_evidence: dict[str, list[str]] = {claim: [] for claim in claim_labels}
    blocked_evidence: list[str] = []
    allowed_evidence: list[dict[str, Any]] = []
    for item in evidence:
        evidence_id = str(item.get("evidence_id") or item.get("chunk_id") or "")
        if item.get("citation_allowed") is True:
            allowed_evidence.append(item)
        elif evidence_id:
            blocked_evidence.append(evidence_id)

    for item in allowed_evidence:
        evidence_id = str(item.get("evidence_id") or item.get("chunk_id") or "")
        support_claims = {str(claim) for claim in item.get("support_claims", [])}
        evidence_refs = {evidence_id, str(item.get("chunk_id") or "")}
        for spec in claim_specs:
            if evidence_id and (set(spec["refs"]) & (support_claims | evidence_refs)):
                claim_evidence[spec["label"]].append(evidence_id)

    unsupported = [claim for claim in claim_labels if not claim_evidence.get(claim)]
    conflicting_claims: list[str] = []
    for spec in claim_specs:
        claim = spec["label"]
        values: set[str] = set()
        for item in allowed_evidence:
            support_claims = {str(value) for value in item.get("support_claims", [])}
            if not (set(spec["refs"]) & support_claims):
                continue
            claim_values = item.get("claim_values") or {}
            value_key = claim if claim in claim_values else next((ref for ref in spec["refs"] if ref in claim_values), None)
            if value_key is None:
                continue
            values.add(json.dumps(claim_values[value_key], ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        if len(values) > 1:
            conflicting_claims.append(claim)

    if conflicting_claims:
        support_status = "conflicted"
        conflict_status = "detected"
    elif unsupported:
        support_status = "insufficient"
        conflict_status = "none"
    else:
        support_status = "supported" if claims and allowed_evidence else "insufficient"
        conflict_status = "none"
    return {
        "valid": support_status == "supported",
        "claims": claim_labels,
        "supported_claims": [claim for claim in claim_labels if claim_evidence.get(claim)],
        "unsupported_claims": unsupported,
        "citation_blocked_evidence": blocked_evidence,
        "claim_evidence": claim_evidence,
        "support_status": support_status,
        "conflict_status": conflict_status,
        "conflicting_claims": conflicting_claims,
    }


def search(
    query: str,
    limit: int = 5,
    publication_id: str | None = None,
    strategy: str = "c",
    reference_date: date | None = None,
    filters: Mapping[str, Any] | None = None,
    allow_pre_release: bool = False,
) -> dict[str, Any]:
    if strategy not in RETRIEVAL_STRATEGIES:
        raise ValueError("invalid_retrieval_strategy")
    publication_id = publication_id or get_current_publication().get("id")
    reference_date = reference_date or datetime.now(timezone.utc).date()
    normalized_filters = _normalize_retrieval_filters(filters)
    filter_stats: dict[str, Any] = {
        "applied": normalized_filters,
        "rejections": {},
    }
    rewrite = rewrite_query(query)
    with connect() as db:
        publication_row = db.execute("SELECT status FROM publications WHERE id = ?", (publication_id,)).fetchone() if publication_id else None
    allowed_publication_statuses = PRE_RELEASE_RETRIEVAL_STATUSES if allow_pre_release else PUBLICATION_RETRIEVAL_STATUSES
    if not publication_row or publication_row["status"] not in allowed_publication_statuses:
        evidence_validation = validate_claim_evidence([], [])
        return {
            "candidates": [],
            "selected": None,
            "meta": {
                "strategy": strategy,
                "query_rewrite": rewrite,
                "stages": ["query_rewrite", "hard_filter", "publication_unavailable", "evidence_validation"],
                "candidate_count": 0,
                "sparse_count": 0,
                "dense_count": 0,
                "dense_available": False,
                "retrieval_state": "publication_unavailable",
                "index_status": "publication_unavailable",
                "dense_index_status": "publication_unavailable",
                "reranker_version": None,
                "relation_edge_count": 0,
                "persisted_claim_conflicts": {"status": "none", "conflicts": []},
                "evidence_validation": evidence_validation,
                "filters_applied": filter_stats["applied"],
                "filter_rejections": filter_stats["rejections"],
            },
        }
    health = get_retrieval_health(publication_id)
    index_ready = health["keyword_index"]["status"] == "ready"
    sparse_candidates = (
        _fts_candidates(rewrite["rewritten"], publication_id, limit, reference_date, normalized_filters, filter_stats, allow_pre_release)
        if index_ready
        else []
    )
    dense_candidates: list[dict[str, Any]] = []
    dense_ready = (
        strategy in {"b", "c"}
        and health.get("dense_profile", {}).get("status") == "ready"
        and health.get("dense_index", {}).get("status") == "ready"
    )
    if dense_ready:
        try:
            dense_candidates = _dense_candidates(
                rewrite["rewritten"], publication_id, limit, reference_date, normalized_filters, filter_stats, allow_pre_release
            )
        except EmbeddingUnavailableError:
            dense_ready = False

    selected = sparse_candidates
    stages = ["query_rewrite", "hard_filter"]
    if index_ready:
        stages.append("sparse")
        if strategy in {"b", "c"} and dense_ready:
            by_chunk = {record["chunk_id"]: record for record in [*sparse_candidates, *dense_candidates]}
            sparse_ranking = [record["chunk_id"] for record in sparse_candidates]
            dense_ranking = [record["chunk_id"] for record in dense_candidates]
            rrf = _rrf_scores([sparse_ranking, dense_ranking])
            for chunk_id, record in by_chunk.items():
                record["rrf_score"] = rrf.get(chunk_id, 0.0)
                record["rank_sources"] = [
                    source
                    for source, ranking in (("keyword", sparse_ranking), ("vector", dense_ranking))
                    if chunk_id in ranking
                ]
                record["score"] = record["rrf_score"]
            selected = sorted(by_chunk.values(), key=lambda item: (-item["rrf_score"], item["chunk_id"]))[:limit]
            stages.extend(["dense", "rrf"])
            if strategy == "c":
                stages.extend(["rerank", "relation"])
                for record in selected:
                    record["relation_score"] = float(len(record.get("relation_edges", [])))
                    record["score"] = record["rrf_score"] + (record["relation_score"] / 1000)
                selected.sort(key=lambda item: (-item["score"], item["chunk_id"]))
            retrieval_state = "relation_augmented" if strategy == "c" else "hybrid"
        elif strategy in {"b", "c"}:
            stages.extend(["dense_unavailable", "degraded_sparse"])
            if strategy == "c":
                stages.append("relation_unavailable")
            retrieval_state = "degraded_sparse"
        else:
            retrieval_state = "keyword"
        if strategy == "c" and dense_ready is False and "relation_unavailable" not in stages:
            stages.append("relation_unavailable")
    else:
        stages.append("index_unavailable")
        retrieval_state = "index_unavailable"
    claims = [selected[0]["slug"]] if selected else []
    evidence_validation = validate_claim_evidence(claims, selected)
    persisted_conflicts = detect_claim_conflicts(publication_id) if publication_id else {"status": "none", "conflicts": []}
    if selected and persisted_conflicts["conflicts"]:
        selected_item_id = selected[0]["service_item_id"]
        relevant_conflicts = [
            conflict
            for conflict in persisted_conflicts["conflicts"]
            if conflict["subject"] in {selected_item_id, f"service_item:{selected_item_id}"}
        ]
        if relevant_conflicts:
            evidence_validation = {
                **evidence_validation,
                "valid": False,
                "support_status": "conflicted",
                "conflict_status": "detected",
                "conflicting_claims": sorted(
                    {
                        f"{conflict['subject']}:{conflict['predicate']}"
                        for conflict in relevant_conflicts
                    }
                ),
            }
    stages.append("evidence_validation")
    return {
        "candidates": selected,
        "selected": selected[0] if selected else None,
        "meta": {
            "strategy": strategy,
            "query_rewrite": rewrite,
            "stages": stages,
            "candidate_count": len(selected),
            "sparse_count": len(sparse_candidates),
            "dense_count": len(dense_candidates),
            "dense_available": dense_ready,
            "retrieval_state": retrieval_state,
            "index_status": health["keyword_index"]["status"],
            "dense_index_status": health.get("dense_index", {}).get("status", "missing"),
            "reranker_version": "business-reranker-v1" if strategy == "c" and dense_ready else None,
            "relation_edge_count": sum(len(record["relation_edges"]) for record in selected),
            "persisted_claim_conflicts": persisted_conflicts,
            "evidence_validation": evidence_validation,
            "filters_applied": filter_stats["applied"],
            "filter_rejections": filter_stats["rejections"],
        },
    }


def retrieve(
    query: str,
    limit: int = 5,
    publication_id: str | None = None,
    reference_date: date | None = None,
) -> list[dict[str, Any]]:
    return search(query, limit=limit, publication_id=publication_id, strategy="c", reference_date=reference_date)["candidates"]
