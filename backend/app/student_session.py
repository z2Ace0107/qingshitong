from __future__ import annotations

import os
import re
import secrets

from fastapi import Request, Response


STUDENT_SESSION_COOKIE = "qst_student_session"
_SESSION_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{8,128}$")


def _cookie_secure() -> bool:
    return os.getenv("QST_APP_ENV", "development").strip().lower() in {"production", "public_demo"}


def _valid_session(value: str | None) -> str | None:
    if value and _SESSION_PATTERN.fullmatch(value):
        return value
    return None


def student_session(request: Request, *, create: bool = True) -> tuple[str | None, bool]:
    """Resolve the server-issued browser session used to own student Runs."""
    existing = _valid_session(request.cookies.get(STUDENT_SESSION_COOKIE))
    if existing:
        return existing, False
    if not create:
        return None, False
    return secrets.token_urlsafe(32), True


def set_student_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        STUDENT_SESSION_COOKIE,
        session_id,
        max_age=60 * 60 * 8,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path="/api",
    )
