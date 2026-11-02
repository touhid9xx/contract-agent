"""Liveness endpoint.

F1 deliberately exposes only liveness. Readiness (DB/Redis/Kafka/Ollama checks)
arrives in F5 with the observability skeleton, once the services it must probe
actually exist -- returning a hard-coded "ready" now would be exactly the
placeholder-success behaviour the specification forbids.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter

from app.core.config import get_settings

router = APIRouter(tags=["health"])

#: Process start, used to report uptime. Monotonic so wall-clock jumps are safe.
_STARTED_AT = time.monotonic()


def uptime_seconds() -> float:
    return round(time.monotonic() - _STARTED_AT, 3)


@router.get("/health", summary="Liveness probe")
async def liveness() -> dict[str, Any]:
    """Return 200 while the process is running and able to serve traffic."""
    settings = get_settings()
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": settings.app_version,
        "environment": settings.app_env,
        "uptime_seconds": uptime_seconds(),
    }
