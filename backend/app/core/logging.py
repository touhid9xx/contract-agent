"""structlog configuration with mandatory PII redaction.

Every log record passes through :func:`redact_event_dict` immediately before
rendering. Two complementary strategies are applied:

1. **Key based** — any field whose name denotes PII or a credential is masked
   outright, regardless of value shape.
2. **Content based** — e-mail addresses and plausible phone numbers embedded in
   free-text values are masked, so a message that interpolates a customer
   address cannot leak it.

Redaction is recursive (dicts, lists, tuples) and never mutates its input, so a
caller holding a reference to the dict it logged sees no surprises.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

MASK = "[REDACTED]"

#: Field names that must never be rendered. Matched case-insensitively as a
#: substring so ``customer_email`` / ``refresh_token`` are covered too.
SENSITIVE_KEY_FRAGMENTS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "encryption_key",
    "private_key",
    "cookie",
    "session",
    "email",
    "phone",
    "msisdn",
    "address",
    "dob",
    "date_of_birth",
    "birth",
    "ssn",
    "nid",
    "iban",
    "card_number",
    "cvv",
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Loose candidate; the digit-count / date guard below decides whether it is
# really a phone number, which keeps ISO timestamps and IDs readable.
_PHONE_CANDIDATE_RE = re.compile(r"\+?\d[\d\s().\-]{6,}\d")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")

MAX_REDACTION_DEPTH = 6


def is_sensitive_key(key: object) -> bool:
    """Return True when a field name denotes PII or a credential."""
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS)


def _looks_like_phone(candidate: str) -> bool:
    if _ISO_DATE_RE.match(candidate):
        return False
    digits = sum(char.isdigit() for char in candidate)
    return digits >= 10


def redact_text(value: str) -> str:
    """Mask e-mail addresses and phone-like sequences inside free text."""
    redacted = _EMAIL_RE.sub(MASK, value)
    return _PHONE_CANDIDATE_RE.sub(
        lambda match: MASK if _looks_like_phone(match.group(0)) else match.group(0),
        redacted,
    )


def redact_value(value: Any, *, depth: int = 0) -> Any:
    """Recursively redact a structured value."""
    if depth > MAX_REDACTION_DEPTH:
        return MASK
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            key: MASK if is_sensitive_key(key) else redact_value(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_value(item, depth=depth + 1) for item in value]
    if isinstance(value, (set, frozenset)):
        return [redact_value(item, depth=depth + 1) for item in value]
    return value


def redact_event_dict(
    _logger: object,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """structlog processor: mask PII/credentials, then return a new mapping."""
    return {
        key: MASK if is_sensitive_key(key) else redact_value(value)
        for key, value in event_dict.items()
    }


def configure_logging(*, level: str = "INFO", json_logs: bool = False) -> None:
    """Configure structlog for the whole process.

    Safe to call repeatedly (tests reconfigure per-case); loggers are never
    cached so a reconfiguration takes effect immediately.
    """
    numeric_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redact_event_dict,
    ]

    renderer: Any
    if json_logs:
        renderer = structlog.processors.JSONRenderer(sort_keys=True)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> Any:
    """Return a bound structlog logger, optionally namespaced."""
    return structlog.get_logger(name)
