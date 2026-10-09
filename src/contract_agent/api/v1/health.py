"""Health & readiness endpoints.

Why separate liveness vs readiness?
    - Liveness: is the process alive? (K8s restarts if false)
    - Readiness: can it serve traffic? (LB stops routing if false)

"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request

from contract_agent.config import get_settings
from contract_agent.logging_config import get_logger

router = APIRouter(tags=["health"])
logger = get_logger(__name__)


@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, str]:
    """Always 200 if the process is up."""
    settings = get_settings()
    return {
        "status": "ok",
        "service": settings.app_name,
        "env": settings.app_env,
        "timestamp": datetime.now(tz=UTC).isoformat(),
    }


@router.get("/health/ready", summary="Readiness probe")
async def readiness(request: Request) -> dict[str, object]:
    """Checks dependency readiness. M0: returns placeholders; M4 wires real checks."""
    checks: dict[str, str] = {
        "db": "not_configured",
        "kafka": "not_configured",
        "redis": "not_configured",
        "ollama": "not_configured",
    }
    # M4 will replace these with real probes; we read app.state for now
    kafka_producer = getattr(request.app.state, "kafka_producer", None)
    if kafka_producer is not None:
        checks["kafka"] = "ok"

    overall = "ok"
    return {
        "status": overall,
        "checks": checks,
        "timestamp": datetime.now(tz=UTC).isoformat(),
    }
