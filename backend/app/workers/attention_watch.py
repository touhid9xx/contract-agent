"""The attention watch: runs the attention check on a schedule (gap 22's residue).

:func:`~app.core.attention.attention_report` made the invisible-failure states
*machine-checkable*, and ``python -m app.cli doctor`` gave the reading an exit code.
Neither closed gap 22, and the record says why: a command nobody schedules is worth
little more than the log line it replaces. A cron entry would schedule it, but this
stack ships a Compose file and three worker services, not a crontab — so the check
had no process, and a deployment that forgot to add one looked identical to a
deployment with nothing to report.

This is that process. Each tick reads the three tables, logs exactly one verdict,
and never writes.

Three decisions, each of which could be made wrongly without failing a test:

**It logs; it does not exit non-zero.** ``doctor`` exits ``EXIT_ATTENTION`` because
its consumer is a cron entry or a CI step that acts on the code. Here the consumer
is an orchestrator with ``restart: unless-stopped``, where a non-zero exit is not a
signal — it is a crash loop. A container restarting every few seconds buries the
line that says what is wrong, and the state it is complaining about (a plaintext
field value, a suppressed notification) does not get better by being restarted. So
the verdict is a log event with a stable name and the report's fields attached, and
the process exits 0 unless something unexpected raised.

**A switched-off bus is said once at startup, and it is named.** With the default
``EVENT_TRANSPORT=none`` every outbox event the relay claims is parked, so on a
fresh install the *first* tick after a reminder is already carrying parked rows. If
this logged those as a fault it would send an operator to look for a Kafka problem
they do not have. :attr:`~app.core.attention.AttentionReport.needs_attention`
already refuses to call an explained park a reason; this worker additionally says
once, at startup, which configuration it is running under, so the per-tick lines do
not have to be read in isolation to make sense.

**A failed tick is logged, not fatal.** The database is the only dependency, and a
worker that dies on a transient connection error stops watching exactly when
something is wrong. Same shape as the relay and the scheduler.

What this is **not**: delivery. A log line reaches a person only if something is
reading the log, and nothing in this stack reads it. The remaining half of gap 22 —
somewhere for the verdict to reach a person *by itself* — is still open, and
deliberately so: the obvious answer (an HTTP route serving the same report) cannot
be built without deciding who may see counts that are deliberately read **across**
tenants. See ``D10-39``.
"""

from __future__ import annotations

import signal
import threading
from types import FrameType

import structlog
from sqlalchemy.orm import Session, sessionmaker

from app.core.attention import AttentionReport, attention_report
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import build_engine, build_session_factory, session_scope

log = structlog.get_logger(__name__)


class AttentionWatch:
    """Reads the attention states on a timer and logs the verdict."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: Settings,
        limit: int | None = None,
        poll_interval_seconds: float | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._limit = settings.attention_field_scan_limit if limit is None else limit
        self._poll_interval = (
            settings.attention_poll_interval_seconds
            if poll_interval_seconds is None
            else poll_interval_seconds
        )

    def run_once(self) -> AttentionReport:
        """One tick: read the three tables once and log one verdict.

        Returns the report rather than only logging it, so a test can assert the
        verdict instead of scraping a log line — the same reason
        :class:`~app.workers.notification_scheduler.TickResult` is returned.
        """
        with session_scope(self._session_factory) as session:
            report = attention_report(session, limit=self._limit, settings=self._settings)
        self._record(report)
        return report

    def run_forever(self, stop: threading.Event) -> None:
        """Poll until ``stop`` is set. Intended as the process main loop."""
        log.info(
            "attention_watch_started",
            poll_interval_seconds=self._poll_interval,
            field_scan_limit=self._limit,
            event_transport=self._settings.event_transport,
        )
        if self._settings.event_transport == "none":
            # Said once here rather than on every tick: this is a configuration
            # fact, not a finding, and repeating it would be the noise the module
            # docstring says turns a real alarm into one nobody reads.
            log.warning(
                "attention_watch_bus_is_switched_off",
                event_transport=self._settings.event_transport,
                effect="every outbox event parks and none can drain until a transport is configured",
                verdict="those parks are reported as explained, not as a fault",
            )

        while not stop.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("attention_watch_tick_failed")
            if stop.wait(self._poll_interval):
                break

        log.info("attention_watch_stopped")

    def _record(self, report: AttentionReport) -> None:
        """Log exactly one verdict for one reading, plus a truncation warning.

        Ordered so the loudest true statement wins: a real finding beats an
        explained park, and an explained park beats silence. Two lines per tick
        would double the log volume of a process whose only product is the log.
        """
        if report.field_scan_truncated:
            log.warning(
                "attention_watch_field_scan_truncated",
                scanned=report.fields_scanned,
                limit=self._limit,
                hint="the unsealed count is a floor; raise ATTENTION_FIELD_SCAN_LIMIT",
            )

        if report.needs_attention:
            log.error("attention_watch_needs_attention", **report.as_dict())
        elif report.parks_explained:
            log.info("attention_watch_bus_is_switched_off_parking", **report.as_dict())
        else:
            # ``debug``, not ``info``: a clean board is the normal state, and a
            # process that logs "nothing to do" every five minutes forever is how
            # the one line that matters gets filtered out.
            log.debug("attention_watch_clean", **report.as_dict())


def main() -> int:
    """``python -m app.workers.attention_watch``."""
    settings = get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.json_logs)

    engine = build_engine(settings)
    factory = build_session_factory(engine)

    watch = AttentionWatch(factory, settings=settings)
    stop = threading.Event()

    def _request_stop(signum: int, _frame: FrameType | None) -> None:
        log.info("attention_watch_stopping", signal=signum)
        stop.set()

    # Graceful shutdown: finish the reading in flight, then exit. An attention
    # reading is read-only, so there is nothing to leave half-done; setting the
    # event simply stops the loop before the next tick instead of mid-query.
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    try:
        watch.run_forever(stop)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
