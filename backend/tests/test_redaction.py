from __future__ import annotations

from app.redaction import redact_value


def test_redact_value_masks_secret_fields_even_without_credential_shaped_values():
    value = {
        "authorization": "plain-authorization-value",
        "api_key": "plain-api-key-value",
        "token": "plain-token-value",
        "nested": {"session_token": "plain-session-token"},
        "token_estimate": 42,
        "secret_source": "secret_file",
        "safe": "keep this diagnostic value",
    }

    redacted = redact_value(value)

    assert redacted["authorization"] == "[REDACTED]"
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["token"] == "[REDACTED]"
    assert redacted["nested"]["session_token"] == "[REDACTED]"
    assert redacted["token_estimate"] == 42
    assert redacted["secret_source"] == "secret_file"
    assert redacted["safe"] == "keep this diagnostic value"


def test_redact_text_masks_token_assignment_without_touching_token_estimate():
    value = "token=plain-token-value token_estimate=42"

    redacted = redact_value(value)

    assert redacted == "token=[REDACTED] token_estimate=42"
