"""FastAPI application factory.

The app is created through :func:`create_app` so tests can inject settings and
so no global state is mutated at import time beyond the module-level ``app``
uvicorn needs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.health import router as health_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware, current_correlation

#: Stable machine-readable error codes keyed by HTTP status.
_ERROR_CODES: dict[int, str] = {
    HTTPStatus.BAD_REQUEST: "bad_request",
    HTTPStatus.UNAUTHORIZED: "unauthorized",
    HTTPStatus.FORBIDDEN: "forbidden",
    HTTPStatus.NOT_FOUND: "not_found",
    HTTPStatus.CONFLICT: "conflict",
    HTTPStatus.REQUEST_ENTITY_TOO_LARGE: "payload_too_large",
    HTTPStatus.UNPROCESSABLE_ENTITY: "validation_error",
    HTTPStatus.TOO_MANY_REQUESTS: "rate_limited",
    HTTPStatus.SERVICE_UNAVAILABLE: "service_unavailable",
}

DEFAULT_ERROR_CODE = "internal_error"


def error_payload(
    *,
    code: str,
    message: str,
    details: Any | None = None,
) -> dict[str, Any]:
    """Build the single error envelope used by every endpoint."""
    context = current_correlation()
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": context.get("request_id"),
        "trace_id": context.get("trace_id"),
    }
    if details is not None:
        error["details"] = details
    return {"error": error}


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    app_settings = settings or get_settings()
    configure_logging(level=app_settings.log_level, json_logs=app_settings.json_logs)
    log = structlog.get_logger(__name__)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        log.info(
            "app_startup",
            service=app_settings.app_name,
            version=app_settings.app_version,
            environment=app_settings.app_env,
        )
        yield
        log.info("app_shutdown", service=app_settings.app_name)

    app = FastAPI(
        title="Contract-Expiry Notification Agent API",
        version=app_settings.app_version,
        description=(
            "Contract-lifecycle automation: ingestion, field extraction, human "
            "review, channel selection, reminders, response tracking, escalation."
        ),
        lifespan=lifespan,
        # Interactive docs are a development affordance, not a production surface.
        docs_url=None if app_settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if app_settings.is_production else "/openapi.json",
    )

    app.state.settings = app_settings

    # Order matters: the last middleware added is the outermost one, so
    # RequestContextMiddleware wraps CORS and every response keeps its ids.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-Trace-ID"],
        expose_headers=["X-Request-ID", "X-Trace-ID"],
    )
    app.add_middleware(RequestContextMiddleware)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = _ERROR_CODES.get(exc.status_code, DEFAULT_ERROR_CODE)
        message = str(exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(code=code, message=message),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            {"location": list(error["loc"]), "message": error["msg"], "type": error["type"]}
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            content=error_payload(
                code="validation_error",
                message="Request payload failed validation",
                details=details,
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
        # Log with traceback; never disclose internals to the caller.
        log.exception("unhandled_exception", exc_type=type(exc).__name__)
        return JSONResponse(
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            content=error_payload(
                code=DEFAULT_ERROR_CODE,
                message="An unexpected error occurred",
            ),
        )

    app.include_router(health_router)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, Any]:
        return {
            "service": app_settings.app_name,
            "version": app_settings.app_version,
            "environment": app_settings.app_env,
            "api": app_settings.api_v1_prefix,
        }

    return app


app = create_app()
