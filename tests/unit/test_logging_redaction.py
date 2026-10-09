"""PII redaction tests."""

from __future__ import annotations

from contract_agent.logging_config import redact_pii


def test_redact_top_level_pii() -> None:
    event = {"email": "a@b.com", "phone": "+1234", "user_id": "u1"}
    out = redact_pii(None, "info", event)
    assert out["email"] == "***REDACTED***"
    assert out["phone"] == "***REDACTED***"
    assert out["user_id"] == "u1"


def test_redact_nested_pii() -> None:
    event = {"customer": {"email": "a@b.com", "name": "Alice"}}
    out = redact_pii(None, "info", event)
    assert out["customer"]["email"] == "***REDACTED***"
    assert out["customer"]["name"] == "Alice"


def test_redact_case_insensitive() -> None:
    out = redact_pii(None, "info", {"EMAIL": "a@b.com"})
    assert out["EMAIL"] == "***REDACTED***"
