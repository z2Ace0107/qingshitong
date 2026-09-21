from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, Request, Response

from .db import connect, json_dumps


SESSION_COOKIE = "qst_governance_session"
CSRF_HEADER = "X-CSRF-Token"
SESSION_TTL = timedelta(hours=8)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _configured_tokens() -> dict[str, str]:
    """Return role-bound bootstrap tokens without exposing their values."""
    raw = os.getenv("QST_GOVERNANCE_BOOTSTRAP_TOKENS", "")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(role): str(token)
        for role, token in value.items()
        if isinstance(role, str) and isinstance(token, str) and token
    }


def _public_demo() -> bool:
    return os.getenv("QST_APP_ENV", "development").strip().lower() == "public_demo"


def _cookie_secure() -> bool:
    return os.getenv("QST_APP_ENV", "development").strip().lower() in {"production", "public_demo"}


def _role_for_token(token: str) -> str | None:
    if not token or _public_demo():
        return None
    for role, configured in _configured_tokens().items():
        if hmac.compare_digest(token, configured):
            return role
    return None


def create_session(token: str) -> dict[str, Any]:
    role = _role_for_token(token)
    if not role:
        raise HTTPException(status_code=401, detail={"code": "governance_auth_required", "message": "需要受控治理会话"})
    session_id = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    now = _now()
    expires_at = now + SESSION_TTL
    with connect() as db:
        db.execute(
            """INSERT INTO governance_sessions
               (id, role, csrf_token_hash, created_at, expires_at, revoked_at, last_used_at)
               VALUES (?, ?, ?, ?, ?, NULL, ?)""",
            (session_id, role, _digest(csrf_token), now.isoformat(), expires_at.isoformat(), now.isoformat()),
        )
    return {
        "session_id": session_id,
        "role": role,
        "csrf_token": csrf_token,
        "expires_at": expires_at.isoformat(),
    }


def set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        # R0 retains compatibility URLs under /api/evaluations and /api/publications.
        # They are still protected by the same role-bound session and CSRF checks.
        path="/api",
    )


def revoke_session(session_id: str | None) -> None:
    if not session_id:
        return
    with connect() as db:
        db.execute(
            "UPDATE governance_sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (_now().isoformat(), session_id),
        )


def _session_from_request(request: Request) -> dict[str, Any] | None:
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        return None
    now = _now().isoformat()
    with connect() as db:
        row = db.execute(
            """SELECT id, role, csrf_token_hash, created_at, expires_at, revoked_at
               FROM governance_sessions WHERE id = ?""",
            (session_id,),
        ).fetchone()
        if not row or row["revoked_at"] or row["expires_at"] <= now:
            return None
        db.execute("UPDATE governance_sessions SET last_used_at = ? WHERE id = ?", (now, session_id))
    return dict(row)


def current_session(request: Request) -> dict[str, Any] | None:
    return _session_from_request(request)


def require_session(request: Request, roles: set[str] | None = None) -> dict[str, Any]:
    session = _session_from_request(request)
    if not session:
        raise HTTPException(status_code=401, detail={"code": "governance_auth_required", "message": "需要受控治理会话"})
    if roles is not None and session["role"] not in roles:
        raise HTTPException(status_code=403, detail={"code": "governance_role_forbidden", "message": "当前治理角色不能执行此操作"})
    return session


def require_csrf(request: Request, session: dict[str, Any]) -> str:
    supplied = request.headers.get(CSRF_HEADER, "")
    if not supplied or not hmac.compare_digest(_digest(supplied), session["csrf_token_hash"]):
        raise HTTPException(status_code=403, detail={"code": "csrf_invalid", "message": "请求缺少有效的治理防伪令牌"})
    return supplied


def session_view(session: dict[str, Any], csrf_token: str | None = None) -> dict[str, Any]:
    value = {
        "role": session["role"],
        "expires_at": session["expires_at"],
    }
    if csrf_token is not None:
        value["csrf_token"] = csrf_token
    return value


def rotate_csrf(session: dict[str, Any]) -> str:
    csrf_token = secrets.token_urlsafe(32)
    with connect() as db:
        db.execute(
            "UPDATE governance_sessions SET csrf_token_hash = ?, last_used_at = ? WHERE id = ? AND revoked_at IS NULL",
            (_digest(csrf_token), _now().isoformat(), session["id"]),
        )
    return csrf_token


def audit_session_action(session: dict[str, Any], action: str, entity_type: str, entity_id: str, metadata: dict[str, Any]) -> None:
    now = _now().isoformat()
    safe_metadata = {**metadata, "governance_session_id": session["id"]}
    with connect() as db:
        db.execute(
            "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                f"audit-governance-{secrets.token_hex(8)}",
                "governance",
                action,
                entity_type,
                entity_id,
                json_dumps(safe_metadata),
                now,
            ),
        )
