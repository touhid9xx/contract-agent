"""Prometheus metrics — counters, histograms, gauges.

Why custom metrics?
    - Ops needs: extraction quality, notification throughput, agent decisions, escalations
    - Alerting rules reference these metric names (ops/prometheus/rules)

"""

from __future__ import annotations

from fastapi import FastAPI
from prometheus_client import Counter, Gauge, Histogram
from prometheus_fastapi_instrumentator import Instrumentator

# ------------------------------------------------------------
# EXTRACTION
# ------------------------------------------------------------
extractions_total = Counter(
    "extractions_total",
    "Total extraction pipeline runs",
    labelnames=["status"],  # success | failed | partial
)

extraction_confidence_histogram = Histogram(
    "extraction_confidence",
    "Distribution of extraction confidence scores",
    labelnames=["field"],  # email | phone | address | dob | start_date | end_date
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0),
)

# ------------------------------------------------------------
# NOTIFICATIONS
# ------------------------------------------------------------
notifications_sent_total = Counter(
    "notifications_sent_total",
    "Total notifications sent",
    labelnames=["channel", "status"],  # channel: sms|email|voice ; status: sent|failed|skipped
)

# ------------------------------------------------------------
# ESCALATIONS
# ------------------------------------------------------------
escalations_total = Counter(
    "escalations_total",
    "Total escalations triggered",
    labelnames=["reason"],  # no_response_14d | manual | compliance
)

escalation_email_failed_total = Counter(
    "escalation_email_failed_total",
    "Total escalation email send failures",
)

# ------------------------------------------------------------
# AGENT
# ------------------------------------------------------------
agent_decisions_total = Counter(
    "agent_decisions_total",
    "Total AI agent channel decisions",
    labelnames=["channel", "outcome"],  # outcome: success|fallback|error
)

agent_decision_duration_seconds = Histogram(
    "agent_decision_duration_seconds",
    "Time spent making an agent decision",
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
)

# ------------------------------------------------------------
# VOICE
# ------------------------------------------------------------
voice_calls_total = Counter(
    "voice_calls_total",
    "Total voice calls placed",
    labelnames=["amd_result"],  # HUMAN | VOICEMAIL | IVR | ANSWERING_MACHINE | FAILED
)

# ------------------------------------------------------------
# KAFKA
# ------------------------------------------------------------
kafka_consumer_lag = Gauge(
    "kafka_consumer_lag",
    "Consumer lag per topic/partition",
    labelnames=["topic", "partition"],
)


# ------------------------------------------------------------
# SETUP
# ------------------------------------------------------------
def setup_metrics(app: FastAPI, *, enabled: bool = True) -> None:
    """Attach /metrics endpoint + HTTP metrics instrumentation.

    IMPORTANT: Prometheus client's default registry is process-global. Calling
    `.instrument()` twice on the same app (e.g. in tests) raises DuplicatedTimeseries.
    We guard by checking if the route already exists.
    """
    if not enabled:
        return

    # Idempotency guard — important for tests that recreate the app
    existing_paths = {getattr(r, "path", None) for r in app.routes}
    if "/metrics" in existing_paths:
        return

    Instrumentator(
        should_group_status_codes=False,
        should_ignore_untemplated=True,
        should_respect_env_var=False,
        excluded_handlers=["/metrics", "/health", "/health/ready"],
    ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
