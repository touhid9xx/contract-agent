"""Request context + security headers + CORS middleware.

Why middleware?
    - Every request needs: request_id, timing, correlation context
    - Security headers (CSP, HSTS, X-Frame-Options) set globally
    - CORS handled once for the whole app

"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from contract_agent.config import Settings
from contract_agent.logging_config import bind_request_context, clear_request_context, get_logger

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
PROCESS_TIME_HEADER = "X-Process-Time-Ms"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign request_id, bind structlog context, measure latency."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Reuse inbound request id if present (helps with distributed tracing)
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        request.state.request_id = request_id

        # trace id from OTEL if available
        trace_id: str | None = None
        try:
            from opentelemetry import trace  # local import keeps M0 fast

            span = trace.get_current_span()
            if span is not None and span.get_span_context().is_valid:
                trace_id = format(span.get_span_context().trace_id, "032x")
        except Exception:  # noqa: BLE001
            trace_id = None

        bind_request_context(request_id=request_id, trace_id=trace_id)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            clear_request_context()

        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[PROCESS_TIME_HEADER] = f"{elapsed_ms:.2f}"
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach OWASP-recommended security headers to every response."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        # These are safe defaults for an API; tighten further in prod (M29)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )
        # HSTS only over HTTPS — set in prod behind TLS terminator (M29)
        return response


def register_middleware(app: FastAPI, settings: Settings) -> None:
    """Wire all middleware. Order matters — outermost first."""
    # CORS first (outermost) so preflight never hits auth
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[REQUEST_ID_HEADER, PROCESS_TIME_HEADER],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
