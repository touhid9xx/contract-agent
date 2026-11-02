"""Request-scoped correlation middleware.

A pure-ASGI middleware (not ``BaseHTTPMiddleware``) so it adds no task-group
overhead and cannot swallow responses. It:

* adopts an inbound ``X-Request-ID`` when it is safe, otherwise generates one;
* always generates a trace id (W3C trace context arrives with OpenTelemetry in
  F5, at which point the inbound ``traceparent`` becomes authoritative);
* binds ``request_id``/``trace_id`` into structlog contextvars so every log
  line emitted while handling the request carries them;
* echoes both ids back on the response;
* emits one structured access log line with status and duration.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

import structlog
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: Correlation ids are reflected into response headers and logs, so they are
#: restricted to a safe alphabet to prevent header/log injection.
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")

REQUEST_ID_HEADER = "x-request-id"
TRACE_ID_HEADER = "x-trace-id"


def sanitize_correlation_id(raw: str | None) -> str | None:
    """Return ``raw`` if it is a safe correlation id, else ``None``."""
    if not raw:
        return None
    candidate = raw.strip()
    if not SAFE_ID_RE.match(candidate):
        return None
    return candidate


def new_request_id() -> str:
    return str(uuid.uuid4())


def new_trace_id() -> str:
    # 32 lowercase hex characters keeps the shape of a W3C trace id.
    return uuid.uuid4().hex


class RequestContextMiddleware:
    """Bind request/trace correlation ids and log the request outcome."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }

        request_id = sanitize_correlation_id(headers.get(REQUEST_ID_HEADER)) or new_request_id()
        trace_id = sanitize_correlation_id(headers.get(TRACE_ID_HEADER)) or new_trace_id()

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            trace_id=trace_id,
            method=scope.get("method"),
            path=scope.get("path"),
        )

        status_code: dict[str, int] = {"value": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_code["value"] = int(message["status"])
                response_headers = MutableHeaders(scope=message)
                response_headers[REQUEST_ID_HEADER] = request_id
                response_headers[TRACE_ID_HEADER] = trace_id
            await send(message)

        started = time.perf_counter()
        log = structlog.get_logger(__name__)
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            log.exception(
                "http_request_failed",
                status_code=500,
                duration_ms=duration_ms,
            )
            raise
        else:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            log.info(
                "http_request",
                status_code=status_code["value"],
                duration_ms=duration_ms,
            )


def current_correlation() -> dict[str, Any]:
    """Return the structlog contextvars bound for the current request."""
    return dict(structlog.contextvars.get_contextvars())


def correlation_id_or_new() -> str:
    """The request's correlation id, or a fresh one outside a request context.

    ``AuditLog.correlation_id`` and ``OutboxEvent.correlation_id`` are both
    non-nullable, so an empty string would fail on a column constraint. A call
    made outside a request — a CLI command, a worker tick — has nothing bound in
    the contextvars and must not be able to trigger that.
    """
    bound = current_correlation().get("request_id")
    return bound if isinstance(bound, str) and bound else new_request_id()
