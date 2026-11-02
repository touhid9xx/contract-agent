"""Outbox relay worker.

Reads claimed rows from the outbox and hands them to a transport. It is
deliberately transport-agnostic: the concrete transport is chosen by
``EVENT_TRANSPORT`` in :mod:`app.workers.publishers` (F4), and when none is
configured :class:`UnavailablePublisher` refuses every publish on purpose.

Why refuse rather than no-op? Because a relay that "succeeds" without a broker
is the log-and-drop failure mode the specification forbids. With the refusing
publisher each row is retried with bounded backoff and then parked in the
``failed`` state for an operator, with its last error preserved.

Failure semantics in one place: a publish is recorded in its **own**
transaction, not in the claim transaction. A crash between "published to the
broker" and "marked published" therefore re-publishes on reclaim — at-least-once,
which is what the specification asks for. Consumers de-duplicate on
``event_id``.
"""

from __future__ import annotations

import signal
import socket
import threading
import uuid
from types import FrameType
from typing import Protocol

import structlog
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.models.outbox import OutboxEvent
from app.db.repositories.outbox import OutboxRepository
from app.db.session import build_engine, build_session_factory, session_scope

log = structlog.get_logger(__name__)

#: Column width of ``outbox.locked_by``.
MAX_WORKER_ID_LENGTH = 100


class EventPublisher(Protocol):
    """Transport contract the relay depends on."""

    def publish(self, event: OutboxEvent) -> None:
        """Publish one event, raising on any failure.

        Implementations must not swallow errors: the relay treats "returned
        normally" as "delivered".
        """
        ...


class PublisherUnavailableError(RuntimeError):
    """No event-bus transport is configured for this process."""


class UnavailablePublisher:
    """Explicit stand-in for a transport that does not exist yet.

    Used between F2 and F4. Every call raises; there is no code path in which it
    reports success, so no event can be silently discarded.
    """

    def __init__(self, reason: str = "no event bus transport configured") -> None:
        self._reason = reason

    def publish(self, event: OutboxEvent) -> None:
        raise PublisherUnavailableError(f"{self._reason} (event {event.event_id})")


def default_worker_id() -> str:
    """A stable-ish identity for the lease/fencing column."""
    return f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"[:MAX_WORKER_ID_LENGTH]


class OutboxRelay:
    """Claims due outbox rows and publishes them, one row at a time."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        publisher: EventPublisher,
        *,
        batch_size: int,
        lease_seconds: int,
        max_attempts: int,
        poll_interval_seconds: float,
        worker_id: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._poll_interval = poll_interval_seconds
        self._worker_id = (worker_id or default_worker_id())[:MAX_WORKER_ID_LENGTH]

    @property
    def worker_id(self) -> str:
        return self._worker_id

    # --- internals -------------------------------------------------------
    def _repository(self, session: Session) -> OutboxRepository:
        return OutboxRepository(
            session,
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
            max_attempts=self._max_attempts,
        )

    def _record(self, event_id: int, *, error: str | None) -> OutboxEvent | None:
        """Persist one publish outcome in its own transaction."""
        with session_scope(self._session_factory) as session:
            return self._repository(session).record_outcome(event_id, error=error)

    # --- public API ------------------------------------------------------
    def run_once(self) -> int:
        """One tick. Returns the number of events successfully published."""
        with session_scope(self._session_factory) as session:
            claimed = self._repository(session).claim_due(self._batch_size)

        if not claimed:
            return 0

        published = 0
        for event in claimed:
            event_id = event.id
            event_type = event.event_type
            tenant_id = str(event.tenant_id)

            try:
                self._publisher.publish(event)
            except Exception as exc:  # any transport failure is a publish failure
                error = f"{type(exc).__name__}: {exc}"
                outcome = self._record(event_id, error=error)
                if outcome is None:
                    # The lease expired while we were publishing: another relay
                    # owns the row now. Do not touch it, and say so — silence
                    # here would hide a duplicate-publish window.
                    log.warning(
                        "outbox_lease_lost",
                        worker_id=self._worker_id,
                        outbox_id=event_id,
                        event_type=event_type,
                    )
                    continue
                log.warning(
                    "outbox_publish_failed",
                    worker_id=self._worker_id,
                    outbox_id=event_id,
                    event_type=event_type,
                    tenant_id=tenant_id,
                    attempts=outcome.attempts,
                    will_retry=outcome.status.value == "pending",
                    error=error,
                )
                continue

            outcome = self._record(event_id, error=None)
            if outcome is None:
                log.warning(
                    "outbox_lease_lost",
                    worker_id=self._worker_id,
                    outbox_id=event_id,
                    event_type=event_type,
                )
                continue

            published += 1
            log.info(
                "outbox_published",
                worker_id=self._worker_id,
                outbox_id=event_id,
                event_type=event_type,
                topic=outcome.topic,
                tenant_id=tenant_id,
                attempts=outcome.attempts,
            )

        return published

    def run_forever(self, stop: threading.Event) -> None:
        """Poll until ``stop`` is set. Intended as the process main loop."""
        log.info(
            "outbox_relay_started",
            worker_id=self._worker_id,
            batch_size=self._batch_size,
            lease_seconds=self._lease_seconds,
            poll_interval_seconds=self._poll_interval,
        )
        while not stop.is_set():
            try:
                published = self.run_once()
            except Exception:
                # A transient database or transport fault must not kill the
                # worker; it is logged and the loop backs off.
                log.exception("outbox_relay_tick_failed", worker_id=self._worker_id)
                published = 0

            if not published and stop.wait(self._poll_interval):
                break

        log.info("outbox_relay_stopped", worker_id=self._worker_id)


def main() -> int:
    """``python -m app.workers.outbox_relay``."""
    settings = get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.json_logs)

    engine = build_engine(settings)
    factory = build_session_factory(engine)

    # Imported here rather than at module scope: ``publishers`` imports this
    # module for the EventPublisher protocol, so a top-level import is circular.
    # The selected transport is logged below so an operator can see whether a
    # tick will actually deliver or park rows.
    from app.workers.publishers import build_publisher

    publisher = build_publisher(settings)
    log.info(
        "outbox_relay_transport",
        transport=settings.event_transport,
        publisher=type(publisher).__name__,
    )

    relay = OutboxRelay(
        factory,
        publisher,
        batch_size=settings.outbox_batch_size,
        lease_seconds=settings.outbox_lease_seconds,
        max_attempts=settings.outbox_max_attempts,
        poll_interval_seconds=settings.outbox_poll_interval_seconds,
    )

    stop = threading.Event()

    def _request_stop(signum: int, _frame: FrameType | None) -> None:
        log.info("outbox_relay_stopping", worker_id=relay.worker_id, signal=signum)
        stop.set()

    # Graceful shutdown: finish the row in flight, then exit.
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    try:
        relay.run_forever(stop)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
