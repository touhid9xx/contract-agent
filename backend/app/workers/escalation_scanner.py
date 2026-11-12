"""The escalation scanner: raises an escalation when a customer goes quiet.

Gap 15 gave the escalation queue a writer, but only a human could reach it.
``POST /contracts/{id}/escalation`` opened the clock and the queue rendered it;
nothing on its own noticed that a contract had stopped moving. That left the same
shape of hole gap 22 closed for notifications — a table with a writer and no
reader of the clock — and it is why ``STATUS.md`` recorded "what is missing is a
*trigger*, not a writer". This module is the trigger.

Four decisions are worth stating, because each could be made the other way and
still pass a naive test.

**The evidence is a notification row, not the contract status.** ``ContractStatus``
has a ``reminder_sent`` member that nothing writes: the dispatch worker sends the
message but does not move the contract. A scanner that waited for that status would
wait forever, and a scanner that *set* it would be asserting a second fact ("a
reminder went out") from inside a loop about a third one ("nobody answered"). So
"we contacted them at T" is read from ``notifications.sent_at``, which is the
column :func:`~app.workers.dispatch.dispatch_notification` actually writes, and the
contract status is only used to exclude the states escalation is illegal from.
``reminder_sent`` staying unwritten is recorded in ``STATUS.md`` rather than
papered over here.

**The window and the SLA are different clocks.** ``escalation_no_response_days``
(14 days of silence) decides *whether* to escalate. ``escalation_sla_hours`` (48 h)
starts counting *after* one is raised, and belongs to
:func:`~app.workers.escalation.sla_deadline`. Conflating them would either escalate
on day two or give the operator a deadline that began before they were told.

**The claim and the writes share one transaction.** The candidates are read
``FOR UPDATE SKIP LOCKED`` and mutated before the commit, so two scanner replicas
cannot both open an escalation for the same contract: the second skips the locked
rows rather than blocking. That is the same bargain
:class:`~app.workers.notification_scheduler.NotificationScheduler` makes for one
row, applied to a batch — so a tick is one transaction, not one per contract, and
``escalation_scan_batch_size`` therefore bounds how long locks are held.

**A refusal is logged, not raised.** The claim query already excludes everything
:func:`~app.workers.escalation.refusal_to_raise` refuses, so a refusal means the
query and the policy disagree. The policy wins, the contract is skipped with a
warning that names the code, and the rest of the batch proceeds — a refusal writes
nothing, so failing the tick would roll back every good escalation beside it.

This worker deliberately does **not** flip ``EscalationStatus.SLA_BREACHED``.
``DECISIONS.md`` D10-20 makes overdue a *derived* reading of ``sla_deadline``
(``is_overdue``), and a stored flag that disagrees with its own deadline is worse
than a computed answer: two sources of truth for "is this late" is how a queue
shows a row as urgent while its deadline says otherwise.
"""

from __future__ import annotations

import signal
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import FrameType

import structlog
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.metrics import ESCALATION_EMAIL_FAILED_TOTAL, ESCALATIONS_TOTAL
from app.db.base import truncate_to_second, utc_now
from app.db.enums import ContractStatus
from app.db.models import Contract
from app.db.repositories.audit import (
    ACTION_ESCALATION_OPENED,
    ACTOR_WORKER,
    AUDIT_ENTITY_ESCALATION,
    AuditLogRepository,
)
from app.db.repositories.escalations import EscalationRepository, claim_silent_contracts
from app.db.session import build_engine, build_session_factory, session_scope
from app.workers.escalation import EscalationRefusedError, refusal_to_raise, sla_deadline
from app.workers.escalation_alerts import (
    EscalationAlerter,
    RaisedEscalation,
    build_escalation_alerter,
)

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ScanResult:
    """What one pass did.

    Returned rather than only logged so a test can assert the verdict instead of
    scraping a log line, and so :meth:`EscalationScanner.run_forever` can tell a
    quiet board from a busy one.
    """

    candidates: int
    opened: int
    #: Refusal codes seen this pass. Expected to be empty — the claim query
    #: excludes every case the policy refuses — so a non-empty tuple means the two
    #: drifted apart, which is worth surfacing rather than swallowing.
    refused: tuple[str, ...] = ()

    @property
    def worked(self) -> bool:
        """Whether the pass changed anything. A quiet pass is the normal case."""
        return self.opened > 0


class EscalationScanner:
    """Opens escalations for contracts that have gone quiet, one batch at a time."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: Settings,
        batch_size: int | None = None,
        poll_interval_seconds: float | None = None,
        alerter: EscalationAlerter | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        # ``None`` means "decide from settings", not "never alert": a deployment that
        # set ESCALATION_ALERT_TO gets its alert without the entry point having to
        # remember to pass one. A caller wanting silence passes a no-op alerter,
        # which is a decision the class can log, rather than leaving it unset.
        self._alerter = build_escalation_alerter(settings) if alerter is None else alerter
        self._batch_size = settings.escalation_scan_batch_size if batch_size is None else batch_size
        self._poll_interval = (
            settings.escalation_scan_poll_interval_seconds
            if poll_interval_seconds is None
            else poll_interval_seconds
        )

    def run_once(self) -> ScanResult:
        """One pass over the silent contracts."""
        return self._scan()

    def run_forever(self, stop: threading.Event) -> None:
        """Poll until ``stop`` is set. Intended as the process main loop."""
        log.info(
            "escalation_scanner_started",
            batch_size=self._batch_size,
            poll_interval_seconds=self._poll_interval,
            no_response_days=self._settings.escalation_no_response_days,
            sla_hours=self._settings.escalation_sla_hours,
            alerting=self._alerter is not None,
        )

        while not stop.is_set():
            try:
                result = self.run_once()
            except Exception:
                # A transient database fault must not kill the worker. Nothing was
                # committed for the failed pass, so the next tick simply re-reads
                # the same candidates.
                log.exception("escalation_scanner_tick_failed")
                result = None

            if result is not None and result.opened:
                log.info(
                    "escalation_scanner_opened_escalations",
                    opened=result.opened,
                    candidates=result.candidates,
                )

            # Always wait: an empty pass found nothing, and a busy one just drained
            # the batch that is due. Waiting zero would spin on a full table.
            if stop.wait(self._poll_interval):
                break

        log.info("escalation_scanner_stopped")

    def _scan(self) -> ScanResult:
        """Claim a batch of silent contracts and escalate each one, in one transaction."""
        now = utc_now()
        cutoff = now - timedelta(days=self._settings.escalation_no_response_days)
        deadline = truncate_to_second(sla_deadline(now, hours=self._settings.escalation_sla_hours))

        raised: list[RaisedEscalation] = []
        refused: list[str] = []
        with session_scope(self._session_factory) as session:
            contracts = claim_silent_contracts(session, cutoff=cutoff, limit=self._batch_size)
            for contract in contracts:
                opened = self._escalate(session, contract, deadline=deadline, refused=refused)
                if opened is not None:
                    raised.append(opened)
            result = ScanResult(
                candidates=len(contracts),
                opened=len(raised),
                refused=tuple(refused),
            )

        # Counted after the commit, not inside the transaction. A batch that rolls
        # back would otherwise leave its escalations in the series, and the graph
        # would report work that no queue holds.
        if raised:
            ESCALATIONS_TOTAL.inc(len(raised), status="opened")
        if refused:
            ESCALATIONS_TOTAL.inc(len(refused), status="refused")

        # Outside the transaction, deliberately. ``session_scope`` has committed by
        # the time control arrives here, so a slow or refusing relay cannot hold row
        # locks over the batch it just wrote and cannot roll back an escalation that
        # already exists. The alert reports a fact; it does not establish one.
        self._alert(raised)
        return result

    def _alert(self, raised: Sequence[RaisedEscalation]) -> None:
        """Tell the operator about a batch this scan opened. Never raises.

        An exception here would make :meth:`run_forever` log a failed tick and
        imply the pass wrote nothing, when in fact it wrote every escalation in
        ``raised``. A refused relay therefore costs one counter and one log line,
        and the queue stays the authority on what happened.
        """
        if not raised:
            return
        if self._alerter is None:
            # Not an error: a deployment may deliberately run log-only. Saying so
            # once per opening batch keeps "no alert address" visible in the log
            # rather than reading as a silent, wired-up success.
            log.warning(
                "escalation_alert_not_configured",
                opened=len(raised),
                effect="set ESCALATION_ALERT_TO to mail a person",
            )
            return
        try:
            self._alerter.raised(raised)
        except Exception:
            ESCALATION_EMAIL_FAILED_TOTAL.inc()
            log.exception(
                "escalation_alert_failed",
                opened=len(raised),
                effect="the escalations are committed and remain in the queue",
            )
        else:
            log.info("escalation_alert_sent", opened=len(raised))

    def _escalate(
        self,
        session: Session,
        contract: Contract,
        *,
        deadline: datetime,
        refused: list[str],
    ) -> RaisedEscalation | None:
        """Open one escalation for ``contract``. Returns the row it opened.

        ``None`` means the policy refused this contract, which the claim query is
        meant to make unreachable. What comes back is what the alert lists, so the
        alert cannot name an escalation that was not written.

        The tenant comes **from the row**, exactly as
        :meth:`~app.workers.notification_scheduler.NotificationScheduler._dispatch_locked`
        takes it: the claim that found this contract is unscoped, so the row's own
        ``tenant_id`` is the only tenancy fact available, and every follow-up query
        is scoped by it.

        The audit row is written in the same shape as
        :func:`~app.api.v1.escalations.raise_escalation`'s — ``before`` names the
        contract's previous status, ``after`` carries the escalation's status, the
        deadline and the new contract status. Two writers of one action should
        leave rows an auditor can read as one series, differing only in
        ``actor_type`` (``worker`` here, ``user`` there).
        """
        tenant_id = contract.tenant_id
        escalations = EscalationRepository(session, tenant_id)
        previous_status = contract.status

        try:
            refusal_to_raise(contract, escalations.open_for_contract(contract.id))
        except EscalationRefusedError as exc:
            # Not raised: a refusal writes nothing, so aborting the batch would roll
            # back every good escalation beside this one. The claim query is meant to
            # make this unreachable, which is exactly why it is logged loudly.
            refused.append(exc.code)
            log.warning(
                "escalation_scanner_refused",
                contract_id=contract.id,
                tenant_id=str(tenant_id),
                code=exc.code,
                effect="skipped; the claim query and the policy disagree",
            )
            return None

        correlation_id = str(uuid.uuid4())
        escalation = escalations.create(
            contract_id=contract.id,
            correlation_id=correlation_id,
            deadline=deadline,
        )
        contract.status = ContractStatus.ESCALATED
        session.flush()
        AuditLogRepository(session, tenant_id).record(
            correlation_id=correlation_id,
            actor_type=ACTOR_WORKER,
            actor_id=None,
            action=ACTION_ESCALATION_OPENED,
            entity=AUDIT_ENTITY_ESCALATION,
            entity_id=str(escalation.id),
            before={"contract_status": previous_status.value},
            after={
                "status": escalation.status.value,
                "sla_deadline": deadline.isoformat(),
                "contract_status": contract.status.value,
            },
            reason=f"no contact accepted for {self._settings.escalation_no_response_days}d",
        )
        log.info(
            "escalation_scanner_opened",
            contract_id=contract.id,
            escalation_id=escalation.id,
            tenant_id=str(tenant_id),
            sla_hours=self._settings.escalation_sla_hours,
        )
        return RaisedEscalation(
            tenant_id=tenant_id,
            contract_id=contract.id,
            escalation_id=escalation.id,
            sla_deadline=deadline,
        )


def main() -> int:
    """``python -m app.workers.escalation_scanner``."""
    settings = get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.json_logs)

    engine = build_engine(settings)
    factory = build_session_factory(engine)

    scanner = EscalationScanner(factory, settings=settings)
    stop = threading.Event()

    def _request_stop(signum: int, _frame: FrameType | None) -> None:
        log.info("escalation_scanner_stopping", signal=signum)
        stop.set()

    # Graceful shutdown: finish the pass in flight, then exit. A pass is one
    # transaction, so "finish" means "commit or roll back", never "half wrote".
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    try:
        scanner.run_forever(stop)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
