"""Application exception hierarchy + FastAPI handlers.

Why a custom hierarchy?
    - One consistent JSON error shape everywhere
    - Each error carries HTTP status + machine-readable code
    - Global handlers → no try/except soup in route bodies
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

from contract_agent.logging_config import get_logger, request_id_var

logger = get_logger(__name__)


# ============================================================
# BASE
# ============================================================
class AppError(Exception):
    """Base class for all application errors."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"
    message: str = "An unexpected error occurred"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        detail: Any = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.detail = detail
        super().__init__(self.message)


# ============================================================
# 4xx CLIENT ERRORS
# ============================================================
class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "Resource not found"


class ValidationError(AppError):
    # NOTE: Starlette 0.48+ deprecated HTTP_422_UNPROCESSABLE_ENTITY
    # in favor of HTTP_422_UNPROCESSABLE_CONTENT (RFC 9110).
    # Use literal 422 for compatibility across Starlette versions.
    status_code = 422
    code = "validation_error"
    message = "Validation failed"


class AuthError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "auth_error"
    message = "Authentication failed"


class ForbiddenError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"
    message = "Insufficient permissions"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "Resource conflict"


class IdempotencyError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "duplicate"
    message = "Duplicate request"


class RateLimitError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"
    message = "Too many requests"


# ============================================================
# 5xx / DOMAIN ERRORS
# ============================================================
class ExtractionError(AppError):
    status_code = 422  # see note in ValidationError
    code = "extraction_failed"
    message = "Extraction pipeline failed"


class NotificationError(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "notification_failed"
    message = "Notification dispatch failed"


class EscalationError(AppError):
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = "escalation_failed"
    message = "Escalation could not be triggered"


class ComplianceError(AppError):
    status_code = status.HTTP_451_UNAVAILABLE_FOR_LEGAL_REASONS
    code = "compliance_blocked"
    message = "Action blocked by compliance policy"


# ============================================================
# ERROR PAYLOAD BUILDER
# ============================================================
def _error_payload(
    *,
    code: str,
    status_code: int,
    detail: Any,
    path: str,
    request_id: str | None,
) -> dict[str, Any]:
    return {
        "error": code,
        "status": status_code,
        "detail": detail,
        "path": path,
        "request_id": request_id,
    }


# ============================================================
# REGISTER HANDLERS
# ============================================================
def register_exception_handlers(app: FastAPI) -> None:
    """Attach global exception handlers. Called once in main.py."""

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        rid = request_id_var.get()
        logger.warning(
            "app_error",
            code=exc.code,
            status=exc.status_code,
            detail=exc.detail or exc.message,
            request_id=rid,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(
                code=exc.code,
                status_code=exc.status_code,
                detail=exc.detail or exc.message,
                path=str(_request.url.path),
                request_id=rid,
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        rid = request_id_var.get()
        logger.info("request_validation_error", errors=exc.errors(), request_id=rid)
        return JSONResponse(
            status_code=422,
            content=_error_payload(
                code="validation_error",
                status_code=422,
                detail=exc.errors(),
                path=str(_request.url.path),
                request_id=rid,
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        rid = request_id_var.get()
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(
                code="http_error",
                status_code=exc.status_code,
                detail=exc.detail,
                path=str(_request.url.path),
                request_id=rid,
            ),
        )

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limit_handler(_request: Request, exc: RateLimitExceeded) -> JSONResponse:
        rid = request_id_var.get()
        logger.warning("rate_limit_exceeded", request_id=rid, detail=str(exc.detail))
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content=_error_payload(
                code="rate_limited",
                status_code=429,
                detail="Too many requests",
                path=str(_request.url.path),
                request_id=rid,
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(_request: Request, exc: Exception) -> JSONResponse:
        rid = request_id_var.get()
        logger.exception("unhandled_exception", request_id=rid, error=str(exc))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_payload(
                code="internal_error",
                status_code=500,
                detail="Internal server error",
                path=str(_request.url.path),
                request_id=rid,
            ),
        )
