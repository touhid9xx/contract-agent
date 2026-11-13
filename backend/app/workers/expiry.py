"""Close contracts whose end date has passed.

The product is anchored to one column and the pipe only reads it forward:
:func:`app.workers.dispatch.milestone_for` stops finding a milestone once a contract
leaves its window, and nothing ever wrote down that the term had *ended*.
``ContractStatus.EXPIRED`` has existed since the first migration with no code path
producing it, so a contract that quietly stopped being remindable stayed ``active``
for ever — counted on a dashboard as live, still eligible for an escalation chase,
and still reading to :func:`app.workers.dispatch.assert_may_contact` as a file a
human had not closed.

The consequence that mattered was at the boundary where messages are made.
``build_notification`` computes "today" in the customer's zone, and
``milestone_for`` returns the *tightest* milestone already due — which, for a
contract whose end date is in the past, is every one of them. So asking for a
reminder on an expired contract produced one: a ``T-14`` row dated before the term
ended, claimed on the next scheduler tick because its moment had already passed. The
reminder itself was the false statement.
:class:`app.workers.reminder_planner.ReminderPlanner` narrowed its window so it no
longer *reaches* those contracts, and left this note behind: they leave that scan
silently, and "closing that needs an expiry transition, not a wider window".

This worker is that transition, and only that. It leases contracts whose end date is
at or before today in UTC, decides per row whether that day has passed where the
*customer* is, and moves the status to ``expired`` with one audit row naming the
status and the date that closed it. It sends nothing, plans nothing and deletes
nothing: it changes only the field the rest of the system already reads.

Two deliberate limits, both recorded in D10-98:

``expired`` is terminal here.
    If a reviewer later corrects ``end_date`` to a future date, the contract stays
    ``expired``. The date was wrong, so the status was wrong — but reopening a closed
    file is a decision for a person who can see why it was closed, not something a
    clock should do because a column moved.

``escalated`` and ``responded`` are never closed.
    The query excludes them (see
    :data:`app.db.repositories.contracts.EXPIRABLE_CONTRACT_STATUSES`). A contract
    under human escalation, or one a customer answered, has someone's attention
    already, and a status write would replace that with a clock's verdict.
"""

from __future__ import annotations

import argparse
import signal
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import FrameType

import structlog
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.metrics import CONTRACTS_EXPIRED_TOTAL
from app.db.base import utc_now
from app.db.enums import ContractStatus
from app.db.models import Contract
from app.db.repositories.audit import (
    ACTION_CONTRACT_EXPIRED,
    ACTOR_WORKER,
    AUDIT_ENTITY_CONTRACT,
    AuditLogRepository,
)
from app.db.repositories.contracts import (
    EXPIRABLE_CONTRACT_STATUSES,
    claim_expired_candidates,
)
from app.db.session import build_engine, build_session_factory, session_scope
from app.workers.dispatch import DispatchRefusedError, contract_today
from app.workers.metrics_server import publish_metrics

log = structlog.get_logger(__name__)


def _utc_date(moment: datetime) -> date:
    """The calendar date of ``moment`` in UTC.

    The same three-line rule the reminder planner applies, and duplicated rather
    than imported: it is not a policy and it has no second interpretation to get
    wrong, so the only thing sharing it would buy is a coupling between two
    processes over how to read ``.date()`` off an aware datetime.
    """
    if moment.tzinfo is None:
        return moment.date()
    return moment.astimezone(UTC).date()


def is_expired(contract: Contract, *, now: datetime) -> bool:
    """Whether this contract's term has ended, on the customer's own calendar.

    Strictly before today: a contract ending today is still live today, and closing
    it early would make a reminder sent this morning about a file the dashboard
    already calls closed. The comparison is against
    :func:`app.workers.dispatch.contract_today`, not the server's date, because a
    contract ending 2025-04-01 is over when 2025-04-02 starts where the customer is —
    which may be up to fourteen hours before or after it starts in UTC.

    Raises :class:`app.workers.dispatch.DispatchRefusedError` for an unresolvable
    zone. Deliberately not "assume UTC": guessing would close the contract a day
    early or a day late, and a day either side of a term end is a message somebody
    reads.
    """
    if contract.end_date is None:
        return False
    return contract.end_date < contract_today(contract, now=now)


@dataclass(frozen=True, slots=True)
class SweepResult:
    """What one page (or one drained run) did.

    Returned rather than only logged so a test can assert the verdict instead of
    scraping a log line. ``still_current`` is not a failure: the lease deliberately
    over-selects by a calendar day, and on a UTC-morning tick it is the normal
    reading.
    """

    candidates: int = 0
    #: Contracts closed, by id, in the order the page returned them.
    expired: tuple[str, ...] = ()
    still_current: int = 0
    #: Contracts whose time zone could not be resolved. Not closed, and not silent:
    #: a standing set of these is the one thing about this worker that needs a person.
    unknown_timezone: tuple[str, ...] = ()

    @property
    def worked(self) -> bool:
        """Whether the pass changed the world. A quiet pass is the normal case."""
        return bool(self.expired)

    def merge(self, other: SweepResult) -> SweepResult:
        """Fold another page's result in, for :meth:`ExpirySweep.drain`."""
        return SweepResult(
            candidates=self.candidates + other.candidates,
            expired=self.expired + other.expired,
            still_current=self.still_current + other.still_current,
            unknown_timezone=self.unknown_timezone + other.unknown_timezone,
        )


class ExpirySweep:
    """Closes the contracts that have outlived their end date, one page at a time."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: Settings,
        batch_size: int | None = None,
        poll_interval_seconds: float | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._batch_size = settings.expiry_sweep_batch_size if batch_size is None else batch_size
        self._poll_interval = (
            settings.expiry_sweep_poll_interval_seconds
            if poll_interval_seconds is None
            else poll_interval_seconds
        )
        # Where the next page starts. Process memory on purpose, for the reason the
        # planner gives: it is a fairness cursor, not a fact about the data. Unlike
        # the planner's, this one does not have to survive a restart — every row a
        # page closes leaves the predicate, so a reset costs one already-empty page.
        self._cursor = 0

    def run_once(self, *, now: datetime | None = None) -> SweepResult:
        """One page of candidates, closed in a single transaction."""
        return self._sweep_page(utc_now() if now is None else now)

    def drain(self, *, now: datetime | None = None) -> SweepResult:
        """Every candidate, once, for a caller that runs and exits.

        For a cron-style ``--once`` this must cover the whole table: on a first
        deployment against an existing book, "every contract that already expired"
        is the entire backlog, and one page per run would clear it at a page per
        hour while the rest of the system kept treating old files as live.
        """
        moment = utc_now() if now is None else now
        total = SweepResult()
        while True:
            total = total.merge(self._sweep_page(moment))
            if self._cursor == 0:  # a short or empty page: this was the last one
                return total

    def run_forever(self, stop: threading.Event) -> None:
        """Poll until ``stop`` is set. Intended as the process main loop."""
        log.info(
            "expiry_sweep_started",
            batch_size=self._batch_size,
            poll_interval_seconds=self._poll_interval,
            statuses=tuple(status.value for status in EXPIRABLE_CONTRACT_STATUSES),
        )

        while not stop.is_set():
            try:
                result = self.run_once()
            except Exception:
                # A transient database fault must not kill the worker. Nothing was
                # committed for the failed page, so the next tick re-reads it.
                log.exception("expiry_sweep_tick_failed")
                result = None

            if result is not None:
                self._log_tick(result)

            # Always wait, and for longer than the planner: the event being looked
            # for happens once per contract on a calendar boundary, so a finer
            # interval would buy a query per minute to notice it sooner by minutes.
            if stop.wait(self._poll_interval):
                break

        log.info("expiry_sweep_stopped")

    def _sweep_page(self, moment: datetime) -> SweepResult:
        """Lease one page of contracts and close whichever terms have ended."""
        # One correlation id per pass, not per row: the interesting unit in the log
        # is "this sweep closed these", and a per-row id would make an operator
        # reassemble a single tick from N unrelated traces.
        correlation_id = str(uuid.uuid4())

        expired: list[str] = []
        unknown: list[str] = []
        still_current = 0

        with session_scope(self._session_factory) as session:
            contracts = claim_expired_candidates(
                session,
                on_or_before=_utc_date(moment),
                after_id=self._cursor,
                limit=self._batch_size,
            )
            for contract in contracts:
                try:
                    ended = is_expired(contract, now=moment)
                except DispatchRefusedError:
                    # Left alone rather than guessed at. Counted, because a contract
                    # whose zone cannot be resolved will never close on its own and
                    # nothing else in the system will notice.
                    unknown.append(str(contract.id))
                    log.warning(
                        "expiry_sweep_unknown_timezone",
                        contract_id=contract.id,
                        tenant_id=str(contract.tenant_id),
                        timezone=contract.timezone,
                    )
                    continue

                if not ended:
                    still_current += 1
                    continue

                self._close(session, contract, correlation_id=correlation_id)
                expired.append(str(contract.id))

            # Advanced inside the transaction but applied only after it commits,
            # below: a rolled-back page must be re-read, not skipped.
            #
            # ``contracts`` is tested before the index because a page can be empty for
            # a reason other than the end of the table: ``claim_expired_candidates``
            # treats a non-positive ``limit`` as "lease nothing", and with a batch of
            # zero ``len(contracts) == self._batch_size`` is ``0 == 0``. Settings
            # forbid a zero batch, but the constructor takes an override and the
            # repository guards the case, so the page must survive its own output.
            next_cursor = (
                contracts[-1].id if contracts and len(contracts) == self._batch_size else 0
            )

        self._cursor = next_cursor

        # Counted after the commit, for the reason the planner gives: a page that
        # rolled back would otherwise leave its closures in the series and the graph
        # would report a status change no row holds.
        if expired:
            CONTRACTS_EXPIRED_TOTAL.inc(len(expired), outcome="expired")
        if still_current:
            CONTRACTS_EXPIRED_TOTAL.inc(still_current, outcome="still_current")
        if unknown:
            CONTRACTS_EXPIRED_TOTAL.inc(len(unknown), outcome="unknown_timezone")

        return SweepResult(
            candidates=len(contracts),
            expired=tuple(expired),
            still_current=still_current,
            unknown_timezone=tuple(unknown),
        )

    def _close(self, session: Session, contract: Contract, *, correlation_id: str) -> None:
        """Move one contract to ``expired`` and say why, in one audit row.

        No outbox event: nothing downstream subscribes to a contract closing, and
        enqueuing one would add a parked row to every deployment whose transport is
        off (gap 25) in exchange for no consumer.

        No ``version`` bump of its own either — ``Contract`` carries
        ``version_id_col``, so assigning ``status`` makes SQLAlchemy bump it. That is
        what stops this write from silently overwriting an operator's concurrent
        edit: the second writer's commit raises ``StaleDataError``.
        """
        previous_status = contract.status
        end_date = contract.end_date
        contract.status = ContractStatus.EXPIRED

        AuditLogRepository(session, contract.tenant_id).record(
            correlation_id=correlation_id,
            actor_type=ACTOR_WORKER,
            actor_id=None,
            action=ACTION_CONTRACT_EXPIRED,
            entity=AUDIT_ENTITY_CONTRACT,
            entity_id=str(contract.id),
            before={
                "status": previous_status.value,
                "end_date": end_date.isoformat() if end_date is not None else None,
            },
            after={
                "status": ContractStatus.EXPIRED.value,
                "end_date": end_date.isoformat() if end_date is not None else None,
            },
            reason=(
                f"end date {end_date.isoformat()} passed in {contract.timezone}"
                if end_date is not None
                else None
            ),
        )

        log.info(
            "expiry_sweep_closed",
            contract_id=contract.id,
            tenant_id=str(contract.tenant_id),
            previous_status=previous_status.value,
            end_date=end_date.isoformat() if end_date is not None else None,
        )

    def _log_tick(self, result: SweepResult) -> None:
        """One line per tick, and nothing at all when there was nothing to lease.

        A tick that closed something is news. A tick that only found rows that are
        still current is the steady state and stays at ``debug`` — the same reading
        the reminder planner and ``attention_watch`` take, so a healthy deployment's
        log is as quiet as its dashboard is flat.
        """
        if not result.candidates:
            return
        logger = log.info if result.worked else log.debug
        logger(
            "expiry_sweep_tick",
            candidates=result.candidates,
            expired=result.expired,
            still_current=result.still_current,
            unknown_timezone=result.unknown_timezone,
        )


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m app.workers.expiry [--once]``.

    ``--once`` drains every candidate and exits, for a deployment that would rather
    run the sweep from cron than hold a process open. It is also the sane first move
    on an existing book: one run closes every term that already ended, instead of
    the hourly tick working through them for a day while the rest of the system
    reads those contracts as live.
    """
    parser = argparse.ArgumentParser(prog="python -m app.workers.expiry")
    parser.add_argument(
        "--once",
        action="store_true",
        help="close every expired candidate once and exit, instead of polling",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.json_logs)

    engine = build_engine(settings)
    factory = build_session_factory(engine)
    sweep = ExpirySweep(factory, settings=settings)

    try:
        if args.once:
            result = sweep.drain()
            log.info(
                "expiry_sweep_drained",
                candidates=result.candidates,
                expired=result.expired,
                still_current=result.still_current,
                unknown_timezone=result.unknown_timezone,
            )
            return 0

        stop = threading.Event()

        def _request_stop(signum: int, _frame: FrameType | None) -> None:
            log.info("expiry_sweep_stopping", signal=signum)
            stop.set()

        # Graceful shutdown: finish the page in flight, then exit. A page is one
        # transaction, so "finish" means "commit or roll back", never "half wrote".
        signal.signal(signal.SIGINT, _request_stop)
        signal.signal(signal.SIGTERM, _request_stop)
        # Served on the polling path only: a ``--once`` run finishes in seconds and
        # must not fight a long-running sweep for the same port.
        with publish_metrics(settings, worker="expiry"):
            sweep.run_forever(stop)
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
