"""The reminder planner: the daily scan that writes the notification rows.

Everything downstream of a reminder already takes its input from a ``notifications``
row: :class:`~app.workers.notification_scheduler.NotificationScheduler` claims the
due ones, :func:`~app.workers.dispatch.dispatch_notification` sends one, and
:class:`~app.workers.escalation_scanner.EscalationScanner` measures silence from
``sent_at``. Nothing wrote those rows except a client calling
``POST /api/v1/notifications`` once per contract, which made "remind 60, 30 and 14
days before expiry" a promise the deployment kept by hand. This module is the clock.

The product rule it implements is the specification's: a **daily scan plus a date
compare**, never a delayed message. ``milestone_for`` is what decides which of the
three milestones is due, and it always picks the *most urgent* one that is already
due — so a contract discovered forty days out gets ``T-30`` rather than a ``T-60``
that would have been true a fortnight ago. The message bodies are milestone-agnostic
for the same reason: they name the expiry date, not the number of days, so a late
send is still a true statement.

Five decisions are worth stating, because each could be made the other way and still
pass a naive test.

**The planner derives a channel when nobody has chosen one, and only then.** A
contract that has never been through the agent has no ``AgentDecision``, and without
one there is no channel to send on — leaving the loop with nothing to do, which is
the hole this slice closes. So the planner calls the same
:func:`~app.workers.decision.build_decision` the ``POST /agent/decisions`` route
calls, and persists the row. It does **not** re-derive the channel anywhere: there is
one policy, and the planner is one of its two callers. When a decision already
exists, its channel wins even if the policy would now choose differently, because the
latest decision may be an operator's correction and a scan is not entitled to
overwrite one.

**A decision flagged for review blocks the reminder, and the planner does not overrule
it.** ``build_notification`` refuses ``NEEDS_REVIEW`` and the planner counts the
refusal. Re-deriving around the flag would be the planner deciding that a human's
"look at this first" meant nothing.

**Progress comes from the id cursor, not from the predicate.** A contract that is
refused — pipeline blocked on an unapproved field, no consent — still matches the
claim query on the next tick, and it will keep matching until a person acts. With a
batch sized smaller than that blocked set, ordering by anything stable would starve
every contract behind it forever while the tick reported itself healthy. So each page
is claimed ``id > cursor`` and the cursor advances; a partial page wraps it back to
zero. ``run_forever`` therefore examines successive pages, and ``drain`` examines
every page. That is the one respect in which this differs from
:func:`~app.db.repositories.escalations.claim_silent_contracts`, where a raised
escalation stops matching and the predicate alone is enough.

**Refusals are counted and logged at debug, not warned.** The escalation scanner
warns because its claim query excludes everything its policy refuses, so a refusal
there means the two drifted apart. Here a refusal is the *expected* answer for a
correctly-configured deployment: a contract awaiting review, or one that has not
consented, is supposed to be refused on every tick until somebody changes something.
Warning on each would fill the log with the product working. One ``info`` line
summarises a tick in which anything happened; the per-contract reason stays at
``debug`` with the refusal *code* and never its ``details``.

**It writes no audit row.** The escalation scanner does, because it changes a
contract's status. This one changes nothing a person can see except that a reminder
is now scheduled, and that fact *is* the row: ``notifications`` already answers "who
was contacted, when, on which channel, from which decision". An audit entry per
planned reminder would duplicate a queryable table and would have to be kept in step
with it.

Deliberately absent: the advisory model. ``POST /agent/decisions`` may consult
:mod:`app.workers.advisory` to re-point a deterministic choice; an unattended tick
does not, because a model call per contract per tick is a cost and a failure mode
nobody asked the clock to take on. An operator who wants the advisory opinion runs the
route, and its decision then wins here like any other.
"""

from __future__ import annotations

import argparse
import signal
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import FrameType

import structlog
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.metrics import REMINDER_PLANS_TOTAL
from app.db.base import utc_now
from app.db.models import Contract
from app.db.repositories.agent import AgentDecisionRepository
from app.db.repositories.contracts import claim_reminder_candidates
from app.db.repositories.fields import ExtractedFieldRepository
from app.db.repositories.notifications import NotificationRepository
from app.db.session import build_engine, build_session_factory, session_scope
from app.workers.decision import DecisionRefusedError, build_decision
from app.workers.dispatch import DEFAULT_MILESTONE_DAYS, build_notification
from app.workers.metrics_server import publish_metrics

log = structlog.get_logger(__name__)

#: The loosest milestone, in days before expiry. Derived from the policy table rather
#: than written as ``60`` so adding a ``T-90`` milestone widens the scan automatically
#: — a hard-coded bound here would silently stop scanning far enough ahead, and the
#: symptom of that is a reminder that never arrives.
WINDOW_DAYS: int = max(days for _, days in DEFAULT_MILESTONE_DAYS)

#: How far past its end date a contract is still worth reminding, in days.
#:
#: Not zero, and not tuning. ``milestone_for`` compares against the contract's **local**
#: today, which can be one calendar day ahead of the UTC date this scan reads, so a
#: contract whose end date is "yesterday" in UTC may still be expiring *today* in its
#: own timezone. The same slack widens the far end of the window for a zone one day
#: behind UTC.
#:
#: One day and no more, because a longer grace would send "a contract we hold for you
#: is approaching its expiry date" about a contract that already expired — the one
#: statement the message makes that would then be false. The grace cannot hide a lapse
#: either: :class:`app.workers.expiry.ExpirySweep` moves a contract to ``expired`` on
#: the first day after its term ends in its own zone, and ``expired`` is outside
#: :data:`app.db.repositories.contracts.REMINDABLE_CONTRACT_STATUSES`, so a stale end
#: date leaves this scan by status rather than by window.
EXPIRY_GRACE_DAYS: int = 1


def _utc_date(moment: datetime) -> date:
    """The calendar date of ``moment`` in UTC.

    ``utc_now`` returns a naive UTC datetime, which is what every other worker passes
    around, so the common path is just ``.date()``. The timezone branch exists because
    a test (or a caller) may hand in an aware value, and reading the date off *that*
    would shift the window by the offset rather than by the calendar.
    """
    if moment.tzinfo is None:
        return moment.date()
    return moment.astimezone(UTC).date()


@dataclass(frozen=True, slots=True)
class PlanResult:
    """What one page (or one drained run) did.

    Returned rather than only logged so a test can assert the verdict instead of
    scraping a log line. ``already_planned`` is not a failure: it is the steady state,
    and on a deployment that has been running for a week it should be the only non-zero
    number.
    """

    candidates: int = 0
    planned: int = 0
    already_planned: int = 0
    #: Refusal codes seen, one entry per refused contract. Duplicates are kept: three
    #: contracts blocked on review is a different reading from one, and the summary
    #: line groups them.
    refused: tuple[str, ...] = ()

    @property
    def worked(self) -> bool:
        """Whether the pass changed the world. A quiet pass is the normal case."""
        return self.planned > 0

    def merge(self, other: PlanResult) -> PlanResult:
        """Fold another page's result in, for :meth:`ReminderPlanner.drain`."""
        return PlanResult(
            candidates=self.candidates + other.candidates,
            planned=self.planned + other.planned,
            already_planned=self.already_planned + other.already_planned,
            refused=self.refused + other.refused,
        )


class ReminderPlanner:
    """Plans the reminders that are due today, one page of contracts at a time."""

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
        self._batch_size = settings.reminder_scan_batch_size if batch_size is None else batch_size
        self._poll_interval = (
            settings.reminder_scan_poll_interval_seconds
            if poll_interval_seconds is None
            else poll_interval_seconds
        )
        # Where the next page starts. Process memory on purpose: it is a fairness
        # cursor, not a fact about the data, so a restart resetting it to zero costs one
        # repeated page and nothing else. Persisting it would mean a migration for a
        # number that is only ever an optimisation.
        self._cursor = 0

    def run_once(self, *, now: datetime | None = None) -> PlanResult:
        """One page of candidates, planned in a single transaction."""
        return self._plan_page(utc_now() if now is None else now)

    def drain(self, *, now: datetime | None = None) -> PlanResult:
        """Every candidate, once, favouring a caller that runs and exits.

        For a cron-style ``--once`` this must cover the whole table: one page per run
        would be the same starvation the cursor exists to prevent, just moved from the
        loop to the scheduler. For the long-running loop it is not used, because a tick
        should bound its own work.
        """
        moment = utc_now() if now is None else now
        total = PlanResult()
        while True:
            total = total.merge(self._plan_page(moment))
            if self._cursor == 0:  # a short or empty page: this was the last one
                return total

    def run_forever(self, stop: threading.Event) -> None:
        """Poll until ``stop`` is set. Intended as the process main loop."""
        log.info(
            "reminder_planner_started",
            batch_size=self._batch_size,
            poll_interval_seconds=self._poll_interval,
            window_days=WINDOW_DAYS,
            expiry_grace_days=EXPIRY_GRACE_DAYS,
        )

        while not stop.is_set():
            try:
                result = self.run_once()
            except Exception:
                # A transient database fault must not kill the worker. Nothing was
                # committed for the failed page, so the next tick re-reads the same
                # candidates — the cursor only moves after a page commits.
                log.exception("reminder_planner_tick_failed")
                result = None

            if result is not None:
                self._log_tick(result)

            # Always wait. A quiet tick found nothing to do, and a busy one just planned
            # a page; waiting zero would spin over a table that has not changed.
            if stop.wait(self._poll_interval):
                break

        log.info("reminder_planner_stopped")

    def _plan_page(self, moment: datetime) -> PlanResult:
        """Claim one page of contracts and plan whatever the policy allows."""
        today = _utc_date(moment)
        earliest = today - timedelta(days=EXPIRY_GRACE_DAYS)
        latest = today + timedelta(days=WINDOW_DAYS + EXPIRY_GRACE_DAYS)

        planned: list[str] = []
        already: list[str] = []
        refused: list[str] = []

        with session_scope(self._session_factory) as session:
            contracts = claim_reminder_candidates(
                session,
                earliest_end_date=earliest,
                latest_end_date=latest,
                after_id=self._cursor,
                limit=self._batch_size,
            )
            for contract in contracts:
                self._plan_contract(
                    session,
                    contract,
                    moment=moment,
                    planned=planned,
                    already=already,
                    refused=refused,
                )
            # Advanced inside the transaction but applied only after it commits, below:
            # a rolled-back page must be re-read, not skipped.
            next_cursor = contracts[-1].id if len(contracts) == self._batch_size else 0

        self._cursor = next_cursor

        # Counted after the commit. A page that rolled back would otherwise leave its
        # reminders in the series, and the graph would report work no queue holds.
        if planned:
            REMINDER_PLANS_TOTAL.inc(len(planned), outcome="planned")
        if already:
            REMINDER_PLANS_TOTAL.inc(len(already), outcome="already_planned")
        if refused:
            REMINDER_PLANS_TOTAL.inc(len(refused), outcome="refused")

        return PlanResult(
            candidates=len(contracts),
            planned=len(planned),
            already_planned=len(already),
            refused=tuple(refused),
        )

    def _plan_contract(
        self,
        session: Session,
        contract: Contract,
        *,
        moment: datetime,
        planned: list[str],
        already: list[str],
        refused: list[str],
    ) -> None:
        """Plan one contract's due milestone, or record why there is nothing to plan.

        Mutates the caller's lists rather than returning a verdict: the page is one
        transaction, so the accounting and the writes have to be decided together.
        """
        tenant_id = contract.tenant_id
        fields = ExtractedFieldRepository(session, tenant_id).for_contract(contract.id)
        decisions = AgentDecisionRepository(session, tenant_id)
        stored = decisions.latest_for_contract(contract.id)

        try:
            # ``stored`` is the authority when it exists, operator correction included.
            # Only a contract no decision has ever been made for is decided here.
            decision = (
                stored
                if stored is not None
                else build_decision(contract, fields, correlation_id=str(uuid.uuid4()))
            )
            row = build_notification(
                contract, decision, now=moment, settings=self._settings, fields=fields
            )
        except DecisionRefusedError as exc:
            # ``DispatchRefusedError`` subclasses this, so one clause covers the channel
            # policy and the dispatch gates. ``details`` is deliberately not logged: it
            # names reasons, and the codes are the stable vocabulary.
            refused.append(exc.code)
            log.debug(
                "reminder_planner_refused",
                contract_id=contract.id,
                tenant_id=str(tenant_id),
                code=exc.code,
            )
            return

        notifications = NotificationRepository(session, tenant_id)
        occupied = notifications.find_slot(
            contract_id=contract.id,
            template=row.template,
            scheduled_date=row.scheduled_date,
        )
        if occupied is not None:
            # The unique constraint is the guarantee; this lookup is why a second tick
            # is a no-op rather than an IntegrityError at commit that would roll back
            # every other contract in the page.
            already.append(str(contract.id))
            return

        # Appended here rather than where it is derived, so a contract refused above
        # leaves no decision row behind to be re-read forever as a stale
        # recommendation. Unconditional on purpose: when a stored decision exists
        # ``decision`` *is* ``stored`` — already persistent in this session, so ``add``
        # appends nothing. The ``if stored is None:`` this replaced read like protection
        # and was not; disabling it changed no behaviour a test could observe.
        decisions.add(decision)
        notifications.add(row)
        planned.append(str(contract.id))
        log.info(
            "reminder_planner_planned",
            contract_id=contract.id,
            tenant_id=str(tenant_id),
            template=row.template,
            scheduled_date=row.scheduled_date.isoformat(),
            channel=str(row.channel),
            derived_decision=stored is None,
        )

    def _log_tick(self, result: PlanResult) -> None:
        """One line per tick, and nothing at all when there is nothing to say.

        A tick that planned something is news. A tick that only found already-planned
        rows is the steady state and stays at ``debug`` — the same reading
        ``attention_watch`` takes of a clean board, so a healthy deployment's log is as
        quiet as its dashboard is flat.
        """
        if not result.candidates:
            return
        logger = log.info if result.worked else log.debug
        logger(
            "reminder_planner_tick",
            candidates=result.candidates,
            planned=result.planned,
            already_planned=result.already_planned,
            refused=sorted(set(result.refused)),
            refused_count=len(result.refused),
        )


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m app.workers.reminder_planner [--once]``.

    ``--once`` drains every candidate and exits, for a deployment that would rather run
    the scan from cron than hold a process open. Without it the worker polls, which is
    how every other scanner in this codebase is deployed.
    """
    parser = argparse.ArgumentParser(prog="python -m app.workers.reminder_planner")
    parser.add_argument(
        "--once",
        action="store_true",
        help="plan every candidate once and exit, instead of polling",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.json_logs)

    engine = build_engine(settings)
    factory = build_session_factory(engine)
    planner = ReminderPlanner(factory, settings=settings)

    try:
        if args.once:
            result = planner.drain()
            log.info(
                "reminder_planner_drained",
                candidates=result.candidates,
                planned=result.planned,
                already_planned=result.already_planned,
                refused_count=len(result.refused),
            )
            return 0

        stop = threading.Event()

        def _request_stop(signum: int, _frame: FrameType | None) -> None:
            log.info("reminder_planner_stopping", signal=signum)
            stop.set()

        # Graceful shutdown: finish the page in flight, then exit. A page is one
        # transaction, so "finish" means "commit or roll back", never "half wrote".
        signal.signal(signal.SIGINT, _request_stop)
        signal.signal(signal.SIGTERM, _request_stop)
        # Served on the polling path only: a ``--once`` run finishes in seconds and
        # must not fight a long-running planner for the same port.
        with publish_metrics(settings, worker="reminder_planner"):
            planner.run_forever(stop)
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
