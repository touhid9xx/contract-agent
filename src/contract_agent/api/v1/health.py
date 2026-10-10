"""Health & readiness endpoints — real component probes.

Why separate liveness vs readiness?
    - Liveness: is the process alive? (K8s restarts if false)
    - Readiness: can it serve traffic? (LB stops routing if false)

Each component has its own endpoint so ops can drill in fast.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Request
from sqlalchemy import text

from contract_agent.config import Settings, get_settings
from contract_agent.logging_config import get_logger

router = APIRouter(tags=["health"])
logger = get_logger(__name__)


# ============================================================
# HELPERS
# ============================================================
def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


async def _check_db(request: Request) -> dict[str, Any]:
    """Ping MySQL with SELECT 1."""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        return {"status": "not_configured", "detail": "session factory missing"}
    try:
        async with factory() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("health_db_failed", error=str(exc))
        return {"status": "error", "detail": str(exc)}


async def _check_kafka(request: Request) -> dict[str, Any]:
    """Check Kafka producer is running."""
    producer = getattr(request.app.state, "kafka_producer", None)
    if producer is None:
        return {"status": "not_configured"}
    if not getattr(producer, "_enabled", False):
        return {"status": "disabled", "detail": "KAFKA_ENABLED=false"}
    if getattr(producer, "_producer", None) is None:
        return {"status": "error", "detail": "producer not started"}
    return {"status": "ok"}


async def _check_redis(request: Request) -> dict[str, Any]:
    """Ping Redis with PING."""
    client = getattr(request.app.state, "redis", None)
    if client is None:
        return {"status": "not_configured"}
    try:
        pong = await client.ping()
        return {"status": "ok" if pong else "error"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("health_redis_failed", error=str(exc))
        return {"status": "error", "detail": str(exc)}


async def _check_ollama(settings: Settings) -> dict[str, Any]:
    """Ping Ollama /api/tags. M25 uses the LLM; here we just probe."""
    if settings.llm_mock:
        return {"status": "mocked", "detail": "LLM_MOCK=true"}
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{settings.ollama_base_url}/api/tags")
            if r.status_code == 200:
                models = [m.get("name") for m in r.json().get("models", [])]
                return {"status": "ok", "models": models}
            return {"status": "error", "detail": f"HTTP {r.status_code}"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("health_ollama_failed", error=str(exc))
        return {"status": "error", "detail": str(exc)}


# ============================================================
# ENDPOINTS
# ============================================================
@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, str]:
    """Always 200 if the process is up."""
    settings = get_settings()
    return {
        "status": "ok",
        "service": settings.app_name,
        "env": settings.app_env,
        "timestamp": _now(),
    }


@router.get("/health/ready", summary="Readiness probe")
async def readiness(request: Request) -> dict[str, Any]:
    """Aggregate readiness — 200 only if all critical components are ok."""
    settings = get_settings()
    db = await _check_db(request)
    kafka = await _check_kafka(request)
    redis = await _check_redis(request)
    ollama = await _check_ollama(settings)

    # Kafka is non-critical in dev (KAFKA_ENABLED=false)
    critical_ok = db["status"] in ("ok", "not_configured") and redis["status"] in (
        "ok",
        "not_configured",
    )

    return {
        "status": "ok" if critical_ok else "degraded",
        "checks": {
            "db": db,
            "kafka": kafka,
            "redis": redis,
            "ollama": ollama,
        },
        "timestamp": _now(),
    }


@router.get("/health/db", summary="Database probe")
async def health_db(request: Request) -> dict[str, Any]:
    return await _check_db(request)


@router.get("/health/kafka", summary="Kafka probe")
async def health_kafka(request: Request) -> dict[str, Any]:
    return await _check_kafka(request)


@router.get("/health/redis", summary="Redis probe")
async def health_redis(request: Request) -> dict[str, Any]:
    return await _check_redis(request)


@router.get("/health/ollama", summary="Ollama probe")
async def health_ollama() -> dict[str, Any]:
    return await _check_ollama(get_settings())
