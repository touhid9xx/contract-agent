"""Readiness probing (F5).

Liveness (``/health``) answers "is this process running?". Readiness
(``/health/ready``) answers "should traffic be sent here?". They are separate
endpoints because merging them turns a database blip into a restart loop: an
orchestrator that reads a failed dependency as a dead process recreates a
healthy API while the dependency is still down.

Only a dependency a request actually needs is probed:

* **the database**, which every route uses;
* **the event transport**, and only when ``EVENT_TRANSPORT`` selects one.

Deliberate omissions — a probe list is a statement about what matters:

* **Redis** is not probed. Nothing in ``backend/app`` uses it (D2-03 chose a row
  lease over a Redis lock), so a Redis outage cannot fail a request. Probing it
  would drop this API out of rotation for a service it never calls.
* **Ollama** is not probed. The extraction worker that will call it does not
  exist yet, and the API never talks to it.
* **The relay's backlog** is not part of readiness. A parked dead-letter row is
  an operator alert, not a reason to stop serving HTTP; coupling them would make
  the API's health depend on the worker's throughput.

Checks are blocking (SQLAlchemy and kafka-python are synchronous), so each runs
in the threadpool under its own timeout. Readiness must answer or fail **fast**:
an orchestrator that gets no answer cannot tell "overloaded" from "dead", and
keeps sending traffic to a wedged process either way.

Responses are coarse on purpose. ``/health/ready`` is unauthenticated, so a
driver error message never reaches the body — it can echo the DSN, including a
host and user. The detail goes to the log, which is redacted; the body says only
"unavailable" or "timeout".
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings
from app.db.session import session_scope

log = structlog.get_logger(__name__)

#: Check names. They are part of the response body, so they are stable strings
#: rather than an enum an orchestrator would have to parse twice.
NAME_DATABASE = "database"
NAME_EVENT_TRANSPORT = "event_transport"

#: Coarse, non-disclosing verdicts. Anything finer would leak internals to an
#: unauthenticated caller.
REASON_UNAVAILABLE = "unavailable"
REASON_TIMEOUT = "timeout"
REASON_NOT_CONFIGURED = "not_configured"

#: A blocking dependency round-trip that raises when the dependency is unusable.
BrokerProbe = Callable[[], None]


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one dependency check."""

    name: str
    ok: bool
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok}
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


@dataclass(frozen=True)
class ReadinessReport:
    """Every check that applies to this process."""

    checks: tuple[CheckResult, ...]

    @property
    def ready(self) -> bool:
        return all(check.ok for check in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {check.name: check.as_dict() for check in self.checks}


def check_database(session_factory: sessionmaker[Session]) -> CheckResult:
    """Round-trip one statement through the real connection path.

    ``SELECT 1`` rather than a pooled-connection inspection: ``pool_pre_ping``
    only proves a connection *was* usable, and the point of readiness is whether
    a query issued now succeeds. It is deliberately not a table read, so the
    check does not depend on migrations having run — a schema-less but reachable
    database is a deployment-ordering problem, not an outage.
    """
    try:
        with session_scope(session_factory) as session:
            session.execute(text("SELECT 1"))
    except Exception as exc:
        # Any failure here means "cannot serve requests": a driver error, a DNS
        # failure, a refused connection. The verdict stays coarse for the caller
        # and the detail goes to the log, which redacts it (see module docstring).
        log.warning(
            "readiness_database_unavailable",
            error=f"{type(exc).__name__}: {exc}",
        )
        return CheckResult(NAME_DATABASE, False, REASON_UNAVAILABLE)
    return CheckResult(NAME_DATABASE, True)


def create_broker_probe(settings: Settings) -> BrokerProbe:
    """Build a blocking round-trip to the configured Kafka brokers.

    ``list_topics`` is the cheapest call that proves a broker **answered**: it
    needs a real metadata response, so a half-open socket that a TCP connect
    would accept is not enough to pass readiness.

    The client is created per probe and closed again. Readiness is a periodic
    request, not a hot path, and a cached client would carry a failed broker's
    state across a recovery — which is exactly the thing being measured.

    ``kafka`` is imported inside this function for the same reason as in
    :mod:`app.workers.publishers` (D4-04): ``EVENT_TRANSPORT=none``, which is the
    default, must never import it.
    """
    from kafka.admin import KafkaAdminClient  # local import: keeps the dep optional

    def probe() -> None:
        client = KafkaAdminClient(
            bootstrap_servers=settings.kafka_servers,
            client_id=f"{settings.kafka_client_id}-readiness",
            # Bounded at the client, not only by the caller's timeout: a thread
            # cannot be cancelled, so the probe must give up on its own too.
            request_timeout_ms=int(settings.readiness_timeout_seconds * 1000),
        )
        try:
            client.list_topics()
        finally:
            client.close()

    return probe


def check_event_transport(
    settings: Settings,
    *,
    probe: BrokerProbe | None = None,
) -> CheckResult:
    """Probe the event bus when this process publishes to one.

    With no transport selected this reports ``ok`` with ``not_configured`` rather
    than failing, because nothing is expected to be delivered: the relay retries
    and parks rows instead (D4-03). :func:`readiness_report` goes further and
    omits the check entirely, so the body cannot be read as "the broker is
    healthy" when no broker is in use.
    """
    if settings.event_transport == "none":
        return CheckResult(NAME_EVENT_TRANSPORT, True, REASON_NOT_CONFIGURED)

    broker_probe = probe or create_broker_probe(settings)
    try:
        broker_probe()
    except Exception as exc:
        log.warning(
            "readiness_event_transport_unavailable",
            transport=settings.event_transport,
            error=f"{type(exc).__name__}: {exc}",
        )
        return CheckResult(NAME_EVENT_TRANSPORT, False, REASON_UNAVAILABLE)
    return CheckResult(NAME_EVENT_TRANSPORT, True)


async def _guarded(
    name: str, operation: Callable[[], CheckResult], limit_seconds: float
) -> CheckResult:
    """Run one blocking check in the threadpool under ``limit_seconds``.

    The limit does not kill the thread — nothing in Python can — it stops the
    *probe* from waiting on it. The operation itself is bounded at the driver as
    well (``request_timeout_ms`` for Kafka), so a hung connect cannot accumulate
    one abandoned thread per scrape.
    """
    try:
        async with asyncio.timeout(limit_seconds):
            return await run_in_threadpool(operation)
    except TimeoutError:
        log.warning("readiness_check_timed_out", check=name, timeout_seconds=limit_seconds)
        return CheckResult(name, False, REASON_TIMEOUT)


async def readiness_report(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    probe: BrokerProbe | None = None,
) -> ReadinessReport:
    """Assemble the readiness verdict for this process.

    Checks run sequentially with a per-check timeout, so the worst case is
    ``len(checks) * readiness_timeout_seconds``. Sequential is correct here:
    there are at most two, they are independent, and a concurrent gather would
    only make the failure mode harder to attribute.
    """
    timeout = settings.readiness_timeout_seconds
    checks = [await _guarded(NAME_DATABASE, lambda: check_database(session_factory), timeout)]

    # Omitted entirely rather than reported green: see check_event_transport.
    if settings.event_transport != "none":
        checks.append(
            await _guarded(
                NAME_EVENT_TRANSPORT,
                lambda: check_event_transport(settings, probe=probe),
                timeout,
            )
        )

    return ReadinessReport(checks=tuple(checks))
