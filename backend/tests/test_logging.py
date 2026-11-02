"""PII redaction guarantees.

These assertions are the regression net for the specification's rule that no raw
customer PII may reach logs, traces or metrics.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from app.core.logging import (
    MASK,
    configure_logging,
    is_sensitive_key,
    redact_event_dict,
    redact_text,
    redact_value,
)


def run_redaction(event_dict: dict[str, Any]) -> dict[str, Any]:
    return dict(redact_event_dict(None, "info", dict(event_dict)))


def test_sensitive_keys_are_detected_by_fragment() -> None:
    assert is_sensitive_key("password")
    assert is_sensitive_key("customer_email")
    assert is_sensitive_key("REFRESH_TOKEN")
    assert is_sensitive_key("encryption_key")
    assert not is_sensitive_key("contract_id")
    assert not is_sensitive_key("duration_ms")
    assert not is_sensitive_key(42)


def test_email_in_free_text_is_masked() -> None:
    assert redact_text("contact a.rahman@corp.example now") == f"contact {MASK} now"


def test_phone_in_free_text_is_masked() -> None:
    assert redact_text("call +8801712345678 today") == f"call {MASK} today"


def test_iso_timestamp_is_not_mistaken_for_a_phone_number() -> None:
    timestamp = "2025-06-14 09:41:00"

    assert redact_text(timestamp) == timestamp


def test_short_digit_run_is_not_treated_as_a_phone_number() -> None:
    assert redact_text("contract #1042 ends 2025-07-08") == "contract #1042 ends 2025-07-08"


def test_sensitive_keys_are_masked_regardless_of_value_shape() -> None:
    redacted = run_redaction(
        {
            "event": "field_reviewed",
            "email": "a.rahman@corp.example",
            "phone": 8801712345678,
            "dob": {"year": 1985, "month": 3},
            "contract_id": 1042,
        }
    )

    assert redacted["email"] == MASK
    assert redacted["phone"] == MASK
    assert redacted["dob"] == MASK
    assert redacted["contract_id"] == 1042
    assert redacted["event"] == "field_reviewed"


def test_redaction_recurses_into_nested_structures() -> None:
    redacted = run_redaction(
        {
            "payload": {
                "customer": {"email": "x@y.example", "note": "ring +8801712345678"},
                "tags": ["a@b.example", "clean"],
            }
        }
    )

    assert redacted["payload"]["customer"]["email"] == MASK
    assert MASK in redacted["payload"]["customer"]["note"]
    assert redacted["payload"]["tags"][0] == MASK
    assert redacted["payload"]["tags"][1] == "clean"


def test_redaction_does_not_mutate_the_input_mapping() -> None:
    original: dict[str, Any] = {"email": "a@b.example", "nested": {"phone": "8801712345678"}}

    run_redaction(original)

    assert original == {"email": "a@b.example", "nested": {"phone": "8801712345678"}}


def test_redact_value_refuses_to_walk_absurdly_deep_structures() -> None:
    deep: Any = "leaf@example.com"
    for _ in range(10):
        deep = {"level": deep}

    redacted = redact_value(deep)

    assert MASK in json.dumps(redacted)


def test_json_log_output_contains_no_raw_pii(capsys: Any) -> None:
    configure_logging(level="INFO", json_logs=True)

    structlog.get_logger("test").info(
        "signup_attempt",
        email="a.rahman@corp.example",
        password="hunter2-secret",
        note="call +8801712345678",
    )

    out = capsys.readouterr().out
    assert "a.rahman@corp.example" not in out
    assert "hunter2-secret" not in out
    assert "8801712345678" not in out

    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["event"] == "signup_attempt"
    assert payload["email"] == MASK
    assert payload["password"] == MASK
    assert payload["level"] == "info"
    assert "timestamp" in payload
