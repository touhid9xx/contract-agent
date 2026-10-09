"""FastAPI application entrypoint — wires everything.

Why lifespan?
    - Start Kafka producer/consumer on boot, stop on shutdown
    - Modern replacement for @app.on_event("startup")

Bangla: এখানে সব middleware, exception handler, metrics, tracing, Kafka wire হয়।
`uvicorn contract_agent.main:app --reload` চালালেই app উঠবে।
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from contract_agent.api.v1.health import router as health_router
from contract_agent.config import get_settings
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

    # --- Kafka producer ---
    producer = KafkaProducer(settings)
    await producer.start()
    app.state.kafka_producer = producer

    # --- Rate limiter ---
    app.state.limiter = build_limiter(settings)

    try:
        yield
    finally:
        await producer.stop()
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

    # Order: middleware → exception handlers → metrics → tracing → routes
    register_middleware(app, settings)
    register_exception_handlers(app)

    if settings.prometheus_enabled:
        setup_metrics(app, enabled=True)

    setup_tracing(app, settings)

    # --- Routes ---
    app.include_router(health_router)  # /health, /health/ready
    # M5+: app.include_router(auth_router, prefix=settings.api_v1_prefix)
    # M8+: app.include_router(contracts_router, prefix=settings.api_v1_prefix)

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
    """Console script entrypoint."""
    import uvicorn

    uvicorn.run(
        "contract_agent.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.is_dev,
    )


if __name__ == "__main__":  # pragma: no cover
    run()
