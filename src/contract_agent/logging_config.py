"""structlog configuration with PII redaction.

Why structlog?
    - Structured JSON in prod, pretty console in dev
    - Processor pipeline → easy to add request_id, trace_id, redaction
    - Never use print() — every log goes through here

"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor

# ------------------------------------------------------------
# PII redaction — NEVER log raw PII
# ------------------------------------------------------------
REDACT_FIELDS: frozenset[str] = frozenset(
    {"email", "phone", "address", "dob", "password", "token", "api_key", "secret"}
)

REDACTED = "***REDACTED***"


def redact_pii(_logger: Any, _method_name: str, event_dict: EventDict) -> EventDict:
    """structlog processor — replaces PII values with ***REDACTED***."""
    for key in list(event_dict.keys()):
        if key.lower() in REDACT_FIELDS:
            event_dict[key] = REDACTED
        # nested dict (e.g. "customer": {"email": ...})
        elif isinstance(event_dict[key], dict):
            for nested_key in list(event_dict[key].keys()):
                if nested_key.lower() in REDACT_FIELDS:
                    event_dict[key][nested_key] = REDACTED
    return event_dict


def _add_app_context(service_name: str, env: str) -> Processor:
    def processor(_logger: Any, _method_name: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service_name)
        event_dict.setdefault("env", env)
        return event_dict

    return processor


def configure_logging(
    *,
    level: str = "INFO",
    json_logs: bool = False,
    service_name: str = "contract-agent",
    env: str = "dev",
) -> None:
    """Configure structlog + stdlib logging. Call once at app startup."""

    # stdlib root logger — so uvicorn/sqlalchemy logs also flow through structlog
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,  # request_id, trace_id, user_id
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_app_context(service_name, env),
        redact_pii,  # ← PII redaction ALWAYS on
        structlog.processors.StackInfoRenderer(),
    ]

    if json_logs:
        shared_processors.append(structlog.processors.format_exc_info)
        shared_processors.append(structlog.processors.JSONRenderer())
    else:
        shared_processors.append(structlog.dev.ConsoleRenderer(colors=True))

    structlog.configure(
        processors=shared_processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logs through structlog's processor chain
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a bound logger. Use this everywhere — never `print()`."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]


# ------------------------------------------------------------
# Correlation-ID helpers (used by middleware)
# ------------------------------------------------------------
import contextvars  # noqa: E402

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)
trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)
user_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("user_id", default=None)
tenant_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tenant_id", default=None
)


def bind_request_context(
    *,
    request_id: str | None = None,
    trace_id: str | None = None,
    user_id: str | None = None,
    tenant_id: str | None = None,
) -> None:
    """Bind correlation IDs for the current async context."""
    if request_id is not None:
        request_id_var.set(request_id)
        structlog.contextvars.bind_contextvars(request_id=request_id)
    if trace_id is not None:
        trace_id_var.set(trace_id)
        structlog.contextvars.bind_contextvars(trace_id=trace_id)
    if user_id is not None:
        user_id_var.set(user_id)
        structlog.contextvars.bind_contextvars(user_id=user_id)
    if tenant_id is not None:
        tenant_id_var.set(tenant_id)
        structlog.contextvars.bind_contextvars(tenant_id=tenant_id)


def clear_request_context() -> None:
    """Clear correlation IDs — call at end of request."""
    request_id_var.set(None)
    trace_id_var.set(None)
    user_id_var.set(None)
    tenant_id_var.set(None)
    structlog.contextvars.clear_contextvars()
