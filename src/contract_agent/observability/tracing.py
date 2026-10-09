"""OpenTelemetry tracing setup — no-op in dev if disabled.

Why tracing?
    - Follow a single request across FastAPI → SQLAlchemy → httpx → Kafka
    - Manual spans for: PDF extraction, LLM agent call, voice call

"""

from __future__ import annotations

from fastapi import FastAPI

from contract_agent.config import Settings
from contract_agent.logging_config import get_logger

logger = get_logger(__name__)


def setup_tracing(app: FastAPI, settings: Settings) -> None:
    """Configure OpenTelemetry. Safe no-op when disabled."""
    if not settings.otel_enabled:
        logger.info("otel_disabled", reason="OTEL_ENABLED=false")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:  # pragma: no cover
        logger.warning("otel_import_failed", error=str(exc))
        return

    resource = Resource.create({"service.name": settings.otel_service_name})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    logger.info(
        "otel_enabled",
        endpoint=settings.otel_exporter_otlp_endpoint,
        service=settings.otel_service_name,
    )
