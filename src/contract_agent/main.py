"""FastAPI application entrypoint — wires everything."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI

from contract_agent.api.v1.health import router as health_router
from contract_agent.config import get_settings
from contract_agent.db.session import create_engine, create_session_factory
from contract_agent.exceptions import register_exception_handlers
from contract_agent.kafka.producer import KafkaProducer
from contract_agent.logging_config import configure_logging, get_logger
from contract_agent.middleware import register_middleware
from contract_agent.observability.metrics import setup_metrics
from contract_agent.observability.tracing import setup_tracing
from contract_agent.security.rate_limit import build_limiter

settings = get_settings()

configure_logging(
    level=settings.app_log_level,
    json_logs=settings.is_prod,
    service_name=settings.app_name,
    env=settings.app_env,
)

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown hooks."""
    logger.info(
        "app_starting",
        env=settings.app_env,
        debug=settings.app_debug,
        kafka_enabled=settings.kafka_enabled,
        otel_enabled=settings.otel_enabled,
    )

    # --- DB engine + session factory ---
    try:
        engine = create_engine(settings)
        app.state.db_engine = engine
        app.state.session_factory = create_session_factory(engine)
        logger.info("db_engine_ready", host=settings.mysql_host, db=settings.mysql_db)
    except Exception as exc:  # noqa: BLE001
        logger.error("db_engine_failed", error=str(exc))
        app.state.db_engine = None
        app.state.session_factory = None

    # --- Redis ---
    try:
        app.state.redis = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_timeout=5.0,
        )
        logger.info("redis_client_ready", url=settings.redis_url)
    except Exception as exc:  # noqa: BLE001
        logger.error("redis_client_failed", error=str(exc))
        app.state.redis = None

    # --- Kafka producer ---
    producer = KafkaProducer(settings)
    await producer.start()
    app.state.kafka_producer = producer

    # --- Rate limiter ---
    app.state.limiter = build_limiter(settings)

    try:
        yield
    finally:
        # --- Shutdown in reverse order ---
        await producer.stop()

        # Narrow Redis via local var — Pylance can't narrow on app.state.redis
        # directly (getattr guard doesn't affect the attribute access).
        redis_client = getattr(app.state, "redis", None)
        if redis_client is not None:
            await redis_client.aclose()
            logger.info("redis_client_closed")

        # Same for DB engine.
        db_engine = getattr(app.state, "db_engine", None)
        if db_engine is not None:
            await db_engine.dispose()
            logger.info("db_engine_disposed")

        logger.info("app_stopped")


def create_app() -> FastAPI:
    """App factory — importable for tests."""
    app = FastAPI(
        title="Contract Agent API",
        version="0.1.0",
        description="AI Contract-Expiry Notification Agent",
        docs_url="/docs" if not settings.is_prod else None,
        redoc_url="/redoc" if not settings.is_prod else None,
        openapi_url="/openapi.json" if not settings.is_prod else None,
        lifespan=lifespan,
    )

    register_middleware(app, settings)
    register_exception_handlers(app)

    if settings.prometheus_enabled:
        setup_metrics(app, enabled=True)

    setup_tracing(app, settings)

    app.include_router(health_router)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "service": settings.app_name,
            "version": "0.1.0",
            "docs": "/docs",
            "health": "/health",
            "metrics": "/metrics",
        }

    return app


app = create_app()


def run() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "contract_agent.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.is_dev,
    )


if __name__ == "__main__":  # pragma: no cover
    run()
