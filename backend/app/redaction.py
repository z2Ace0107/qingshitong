from __future__ import annotations

import re
from typing import Any


_CREDENTIAL_PREFIX_PATTERN = re.compile(
    r"(?i)(bearer\s+|(?:api[_-]?key|access[_-]?token|refresh[_-]?token|session[_-]?token|token)\s*[:=]\s*)[^\s,;]+"
)
_TOKEN_PATTERN = re.compile(r"(?i)(?:sk|qst-test)-[A-Za-z0-9_-]{8,}")
_SAFE_DIAGNOSTIC_KEYS = {"secret_source", "credential_source"}


def _is_sensitive_key(key: object) -> bool:
    """Recognize credential-bearing field names before recursing into values."""
    raw = str(key).strip().lower()
    if raw in {"密钥", "令牌", "凭据", "密码"}:
        return True
    normalized = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    if not normalized or normalized in _SAFE_DIAGNOSTIC_KEYS:
        return False
    parts = set(normalized.split("_"))
    return (
        normalized in {
            "api_key",
            "apikey",
            "authorization",
            "cookie",
            "password",
            "secret",
            "private_key",
            "client_secret",
            "credential",
            "token",
        }
        or normalized.startswith(("api_key_", "authorization_", "cookie_", "set_cookie"))
        or normalized.endswith(("_token", "_secret", "_password", "_credential", "_cookie"))
        or ("api" in parts and "key" in parts)
        or bool(parts & {"authorization", "cookie", "password", "secret", "credential"})
    )


def redact_text(value: str) -> str:
    """Remove common credential-shaped values before persistence or exposure."""
    redacted = _CREDENTIAL_PREFIX_PATTERN.sub(lambda match: f"{match.group(1)}[REDACTED]", value)
    return _TOKEN_PATTERN.sub("[REDACTED]", redacted)


def redact_value(value: Any) -> Any:
    """Apply credential redaction recursively to JSON-compatible values."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _is_sensitive_key(key) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return value
