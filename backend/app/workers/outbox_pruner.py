"""Deletes settled outbox rows on a clock, so retention stops depending on a person.

``cli outbox-prune`` has existed since ``D10-148`` and does exactly one page per
invocation, with the cutoff supplied by the operator. That was deliberate — the
cutoff is a policy about how long this deployment keeps its own delivery history —
but it left the residue ``D10-148`` recorded and gap 25 repeated: *nothing schedules
it*, so on every deployment so far the table grows forever unless somebody remembers
a command. This worker is the scheduler, and only the scheduler: it invents no
retention window, it reads ``OUTBOX_RETENTION_DAYS`` and does nothing until that
setting is non-zero.

Three properties the rest of this file exists to keep:

* **Off means off.** ``OUTBOX_RETENTION_DAYS=0`` (the default) makes ``run_once``
  return a disabled report without touching the database. A stock ``.env.example``
  therefore keeps every row it ever wrote, and turning this on is a visible line in
  a deployment's configuration rather than a behaviour change it never asked for.
* **Only settled rows.** The deletion predicate is ``prune_terminal_before``'s,
  reused rather than restated: rows in ``pending`` or ``publishing`` are outside it,
  so no value of any setting here can delete an event that has not been delivered.
  A parked row is a second switch (``OUTBOX_PRUNE_INCLUDE_FAILED``) because a row in
  ``failed`` is one a person was meant to read and replay, and time is the one
  deletion reason that cannot be undone.
* **A full page is a floor.** The command's one page per run is right for a person
  watching a terminal and wrong for a timer: a backlog of 50 000 rows would drain at
  1 000 a day. The pass repeats until a page comes back short, bounded by
  ``MAX_PRUNE_PAGES`` so one tick cannot hold a transaction open indefinitely; the
  report carries ``truncated`` when the bound was the reason it stopped, for the
  reason ``attention_report`` renders ``field_scan_truncated`` beside
  ``fields_scanned`` — a floor read as a total is the reading that misleads.

The log event names are the command's (``outbox_prune``, ``outbox_prune_nothing_settled``)
so one log query finds both paths to the same deletion.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import FrameType

import structlog
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.base import utc_now
from app.db.repositories.outbox import PrunedEvent, prune_terminal_before
from app.db.session import build_engine, build_session_factory, session_scope
from app.workers.metrics_server import publish_metrics

log = structlog.get_logger(__name__)

#: Pages one tick may delete. Reaching it means the cutoff still covers rows this
#: tick did not reach; the next tick continues from the same cutoff, so a very large
#: backlog drains over several days rather than in one unbounded transaction.
MAX_PRUNE_PAGES = 100


@dataclass(frozen=True)
class PruneReport:
    """What one pass did, in the terms the log line reports it."""

    #: The instant rows had to have settled before to be deleted.
    before: datetime
    pruned: tuple[PrunedEvent, ...]
    #: True when the pass stopped at ``MAX_PRUNE_PAGES`` with a full page, so
    #: ``len(pruned)`` is a floor rather than the whole backlog.
    truncated: bool
    #: True when ``OUTBOX_RETENTION_DAYS`` is zero, so no query ran at all.
    disabled: bool

    @property
    def count(self) -> int:
        return len(self.pruned)


class OutboxPruner:
    """Deletes settled outbox rows older than the configured retention window.

    Every input is a constructor argument with a settings-derived default, so a test
    can fix the clock, shrink the batch to force truncation, and pin the window
    without an environment variable or a patched module-level name.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: Settings,
        retention_days: int | None = None,
        batch_size: int | None = None,
        include_failed: bool | None = None,
        interval_seconds: float | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._retention_days = (
            settings.outbox_retention_days if retention_days is None else retention_days
        )
        self._batch_size = settings.outbox_prune_batch_size if batch_size is None else batch_size
        self._include_failed = (
            settings.outbox_prune_include_failed if include_failed is None else include_failed
        )
        self._interval_seconds = (
            settings.outbox_prune_interval_seconds if interval_seconds is None else interval_seconds
        )
        self._clock = clock

    @property
    def enabled(self) -> bool:
        return self._retention_days > 0

    def run_once(self) -> PruneReport:
        """One pass: every page the cutoff covers, up to ``MAX_PRUNE_PAGES``."""
        now = self._clock()
        if not self.enabled:
            # No cutoff, no query: a disabled pruner must not so much as read the
            # table, so that "off" is provably indistinguishable from absent.
            return PruneReport(before=now, pruned=(), truncated=False, disabled=True)

        before = now - timedelta(days=self._retention_days)
        pruned: list[PrunedEvent] = []
        truncated = False

        for _ in range(MAX_PRUNE_PAGES):
            with session_scope(self._session_factory) as session:
                page = prune_terminal_before(
                    session,
                    before=before,
                    limit=self._batch_size,
                    include_failed=self._include_failed,
                )
            pruned.extend(page)
            truncated = len(page) == self._batch_size
            if not truncated:
                break

        report = PruneReport(
            before=before,
            pruned=tuple(pruned),
            truncated=truncated,
            disabled=False,
        )
        self._record(report)
        return report

    def _record(self, report: PruneReport) -> None:
        """Report the pass, including the one that found nothing.

        The empty pass is logged at ``info`` rather than ``debug`` — unlike
        ``AttentionWatch``'s clean board, which ticks every few minutes — because
        this one runs once a day: a single line a day is the heartbeat that proves
        the timer is alive, and at ``debug`` an operator could not tell a working
        schedule from a container that never started.
        """
        if not report.pruned:
            log.info(
                "outbox_prune_nothing_settled",
                before=report.before.isoformat(),
                retention_days=self._retention_days,
                include_failed=self._include_failed,
            )
            return

        log.info(
            "outbox_prune",
            before=report.before.isoformat(),
            retention_days=self._retention_days,
            include_failed=self._include_failed,
            pruned=report.count,
            truncated=report.truncated,
            events=[{"id": event.id, "status": event.status} for event in report.pruned[:10]],
        )

    def run_forever(self, stop: threading.Event) -> None:
        if self.enabled:
            log.info(
                "outbox_pruner_started",
                retention_days=self._retention_days,
                include_failed=self._include_failed,
                interval_seconds=self._interval_seconds,
            )
        else:
            # Said once, at startup, and never again: with retention off this
            # process has nothing to do, so a per-tick line would be a daily
            # "nothing happened" until the deployment ended.
            log.info(
                "outbox_prune_disabled",
                effect="settled rows are kept indefinitely",
                how_to_enable="set OUTBOX_RETENTION_DAYS to a positive number of days",
            )

        while not stop.is_set():
            if self.enabled:
                try:
                    self.run_once()
                except Exception:
                    # A pass that raises is not a reason to stop pruning for the
                    # rest of the deployment's life; the next tick retries. Nothing
                    # is left half-done, because each page is its own transaction.
                    log.exception("outbox_prune_failed")
            stop.wait(self._interval_seconds)


def main() -> int:
    """``python -m app.workers.outbox_pruner``."""
    settings = get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.json_logs)

    engine = build_engine(settings)
    factory = build_session_factory(engine)
    pruner = OutboxPruner(factory, settings=settings)
    stop = threading.Event()

    def _request_stop(signum: int, _frame: FrameType | None) -> None:
        log.info("outbox_pruner_stopping", signal=signum)
        stop.set()

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    try:
        # No family of its own: every count this worker would publish is one it
        # deletes. It is served anyway because "this process is reachable" is the
        # same fact for all eight workers, and a worker with no listener is the
        # asymmetry that hid the others.
        with publish_metrics(settings, worker="outbox_pruner"):
            pruner.run_forever(stop)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
