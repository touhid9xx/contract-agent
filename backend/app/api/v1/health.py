"""Liveness and readiness endpoints.

``/health`` is liveness: it answers "is the process running?" and deliberately
touches no dependency, so a database blip cannot make an orchestrator restart a
healthy API. ``/health/ready`` is readiness and does probe the dependencies a
request needs — :mod:`app.core.readiness` records what is checked and what is
deliberately not.

Both live outside ``settings.api_v1_prefix`` on purpose: an orchestrator must not
have to track an API version to ask whether the process is alive.
"""

from __future__ import annotations

import time
from http import HTTPStatus
from typing import Any

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import app_session_factory, app_settings
from app.core.config import Settings
from app.core.readiness import readiness_report

router = APIRouter(tags=["health"])

#: Process start, used to report uptime. Monotonic so wall-clock jumps are safe.
_STARTED_AT = time.monotonic()


def uptime_seconds() -> float:
    return round(time.monotonic() - _STARTED_AT, 3)


@router.get("/health", summary="Liveness probe")
async def liveness(settings: Settings = Depends(app_settings)) -> dict[str, Any]:
    """Return 200 while the process is running and able to serve traffic."""
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": settings.app_version,
        "environment": settings.app_env,
        "uptime_seconds": uptime_seconds(),
    }


@router.get("/health/ready", summary="Readiness probe")
async def readiness(
    response: Response,
    settings: Settings = Depends(app_settings),
    session_factory: sessionmaker[Session] = Depends(app_session_factory),
) -> dict[str, Any]:
    """Return 200 when every needed dependency answers, 503 when one does not.

    The status code carries the verdict; the body carries per-check detail so an
    operator can see *which* dependency failed without reading logs. The body
    stays coarse (see :mod:`app.core.readiness`) because this route is
    unauthenticated.
    """
    report = await readiness_report(session_factory, settings)
    if not report.ready:
        response.status_code = HTTPStatus.SERVICE_UNAVAILABLE

    return {
        "status": "ready" if report.ready else "not_ready",
        "service": settings.app_name,
        "version": settings.app_version,
        "environment": settings.app_env,
        "checks": report.as_dict(),
    }
