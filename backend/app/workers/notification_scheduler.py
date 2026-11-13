"""The notification scheduler: makes a due — or deferred — row actually run (gap 22).

Before this module nothing moved a ``pending`` notification. ``POST /notifications``
created rows, ``POST /notifications/{id}/dispatch`` sent one, but no process ever
looked at ``scheduled_at``. Two consequences: a quiet-hours deferral wrote a future
``scheduled_at`` into a table nobody polled, so the send it deferred never happened;
and a row created by the producer sat untouched until a human dispatched it by hand.

What "due" means is deliberately not re-derived here. :func:`claim_next_due` owns
the predicate — ``pending`` and (no due time or one that has passed) — so the
scheduler and the producer cannot disagree about when a row is ready, and a
deferral resumes without anything remembering it happened.

Three decisions worth stating, because each could be made wrongly without failing
a test:

**The lock spans the send.** The claim and the dispatch happen in *one* transaction,
so the ``SELECT ... FOR UPDATE`` taken by :func:`claim_next_due` is still held while
the transport is called. That is what makes two schedulers safe: the second blocks,
then reads the row already ``sent`` and refuses with ``NOT_PENDING``. Releasing the
lock before sending would keep the query count the same and leave the double-send
possible — :func:`~app.db.repositories.notifications.NotificationRepository.claim_for_dispatch`
exists in the route for the same reason. (On SQLite ``FOR UPDATE`` is omitted, so
this is reasoning plus a MySQL integration test, not a SQLite assertion — gap 23.)

**A crash mid-send re-sends.** The commit lands after the transport returns, so a
process that dies between "provider accepted" and "row marked sent" leaves the row
``pending`` and the next tick sends it again. At-least-once, the same bargain the
outbox relay makes, and the reason ``idempotency_key`` is handed to the transport
for a provider to dedupe against.

**A refusal is not always a wait.** The queue is ordered by ``scheduled_at``, so a
row that can never send stays at the head and starves every row behind it. Permanent
refusals are therefore parked ``suppressed`` (``SUPPRESSING_REFUSALS`` in
:mod:`app.workers.dispatch`) and left for ``POST /notifications/{id}/retry``, while
transient ones leave the row ``pending`` and are simply seen again next tick.

No transport is configured in this build, so in production ``run_once`` is a no-op
that says so once at startup rather than per tick — refusing to pretend, exactly as
:class:`~app.workers.dispatch.UnavailableSender` does for the route.
"""

from __future__ import annotations

import signal
import threading
from dataclasses import dataclass
from types import FrameType

import structlog
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.core.crypto import FieldCipher, build_cipher
from app.core.logging import configure_logging
from app.db.base import utc_now
from app.db.enums import NotificationStatus
from app.db.models import Contract, Notification
from app.db.repositories.contracts import ContractRepository
from app.db.repositories.fields import ExtractedFieldRepository
from app.db.repositories.notifications import claim_next_due
from app.db.session import build_engine, build_session_factory, session_scope
from app.workers.dispatch import (
    SUPPRESSING_REFUSALS,
    DispatchRefusedError,
    MessageSender,
    dispatch_notification,
    recipient_for,
)
from app.workers.metrics_server import publish_metrics
from app.workers.senders import build_sender

log = structlog.get_logger(__name__)

#: Recorded on the row when its contract is not in the row's own tenant.
#:
#: :func:`claim_next_due` is one of the codebase's two unscoped reads — the other is
#: :func:`~app.db.repositories.outbox.OutboxRepository.claim_due`, unscoped for the
#: same structural reason — so it can only be safe if the very next read is scoped
#: by the tenant *the row* names and any disagreement stops the row. A dangling
#: ``contract_id`` and a cross-tenant one are the same verdict here on purpose:
#: neither is dispatchable, and telling them apart would need a second unscoped
#: query — which is the thing being avoided.
REFUSAL_TENANT_MISMATCH = "TENANT_MISMATCH"

#: ``notifications.last_error`` is written in the same shape the policy uses.
MAX_ROW_ERROR_CHARS = 500


@dataclass(frozen=True, slots=True)
class TickResult:
    """What one claim-then-dispatch iteration did.

    Returned rather than logged-and-forgotten so the tests can assert the verdict
    instead of scraping a log line, and so :meth:`NotificationScheduler.run_once`
    can tell "drained" from "stuck".
    """

    claimed: bool
    sent: bool = False
    deferred: bool = False
    suppressed: bool = False
    refusal: str | None = None

    @property
    def resolved(self) -> bool:
        """Whether the row stopped being due without an attempt being spent.

        A tick that sent, deferred or parked the row has made progress, so the
        batch continues. One that spent an attempt on a transport that failed has
        not: continuing would re-claim a row that is *still due* and burn its whole
        retry budget inside a single tick, so the batch stops and the next one
        starts a poll interval later. ``notification_retry_max`` then means what it
        reads as — attempts, not attempts per drain.
        """
        return self.sent or self.deferred or self.suppressed


class NotificationScheduler:
    """Dispatches due notification rows, one locked row at a time."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        sender: MessageSender,
        *,
        settings: Settings,
        batch_size: int | None = None,
        poll_interval_seconds: float | None = None,
        cipher: FieldCipher | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._sender = sender
        self._settings = settings
        # Built on first use, not here: an installation with no ``ENCRYPTION_KEY``
        # should fail at the tick that needs it (with a log line naming the reason)
        # rather than refuse to construct a worker that might never be asked to send.
        self._cipher = cipher
        self._batch_size = settings.notification_batch_size if batch_size is None else batch_size
        self._poll_interval = (
            settings.notification_poll_interval_seconds
            if poll_interval_seconds is None
            else poll_interval_seconds
        )

    def _field_cipher(self) -> FieldCipher:
        """The cipher used to read a recipient, built once and reused.

        ``build_cipher`` fails closed on a missing or malformed key; that raises out
        of the tick, where :meth:`run_forever` logs it as a failed tick instead of
        mistaking it for a per-row refusal.
        """
        if self._cipher is None:
            self._cipher = build_cipher(self._settings)
        return self._cipher

    def run_once(self) -> int:
        """One tick: dispatch up to ``batch_size`` rows, returning how many sent.

        Returns early when *no* transport is configured at all. That is not just an
        optimisation: every row would refuse ``SENDER_UNAVAILABLE``, and because the
        head of the queue is claimed first, a single tick would log the same refusal
        for the same row hundreds of times and never touch another row.

        A deployment with only some channels configured is deliberately *not* an
        early return. ``sender.channels`` is per channel, so a due SMS with no gateway
        still gets its own refused tick and its own ``last_error``; skipping the whole
        loop because e-mail works would leave that row pending with no explanation.
        """
        if not self._sender.channels:
            log.debug(
                "notification_scheduler_no_transport",
                sender=type(self._sender).__name__,
            )
            return 0
        if not self._settings.has_encryption_key:
            # Same shape of early return as the missing transport above, for the same
            # reason: since gap 20, no recipient can be read without the key, so every
            # row would refuse and the batch would spend its attempts on that alone.
            log.debug(
                "notification_scheduler_no_encryption_key",
                effect="recipient values cannot be decrypted",
            )
            return 0

        sent = 0
        for _ in range(self._batch_size):
            tick = self._tick()
            if not tick.claimed:
                break
            if tick.sent:
                sent += 1
            if not tick.resolved:
                break
        return sent

    def run_forever(self, stop: threading.Event) -> None:
        """Poll until ``stop`` is set. Intended as the process main loop."""
        log.info(
            "notification_scheduler_started",
            batch_size=self._batch_size,
            poll_interval_seconds=self._poll_interval,
            sender=type(self._sender).__name__,
            transport_channels=sorted(channel.value for channel in self._sender.channels),
        )
        if not self._sender.channels:
            # Said once here rather than every tick: pending rows will not move on
            # their own until an operator configures a transport, and that is the
            # single most useful thing this log can tell them.
            log.warning(
                "notification_scheduler_will_not_send",
                reason="no notification transport is configured",
                effect="due rows stay pending",
            )

        while not stop.is_set():
            try:
                sent = self.run_once()
            except Exception:
                # A transient database or transport fault must not kill the worker;
                # it is logged and the loop backs off.
                log.exception("notification_scheduler_tick_failed")
                sent = 0

            if not sent and stop.wait(self._poll_interval):
                break

        log.info("notification_scheduler_stopped")

    def _tick(self) -> TickResult:
        """Claim one due row and dispatch it, in a single transaction."""
        with session_scope(self._session_factory) as session:
            row = claim_next_due(session)
            if row is None:
                return TickResult(claimed=False)
            return self._dispatch_locked(session, row)

    def _dispatch_locked(self, session: Session, row: Notification) -> TickResult:
        """Dispatch ``row``, which this session holds locked.

        The tenant comes **from the row**, because an unscoped scheduler cannot know
        it before claiming. Every follow-up read is scoped by it, and the contract's
        own tenant is checked by that scoping — a contract belonging to another
        tenant reads as absent here.
        """
        tenant_id = row.tenant_id
        contract = ContractRepository(session, tenant_id).get(Contract, row.contract_id)
        if contract is None:
            reason = f"{REFUSAL_TENANT_MISMATCH}: the contract is not in this notification's tenant"
            self._suppress(row, reason)
            return TickResult(claimed=True, suppressed=True, refusal=REFUSAL_TENANT_MISMATCH)

        fields = ExtractedFieldRepository(session, tenant_id).for_contract(contract.id)

        try:
            outcome = dispatch_notification(
                row,
                contract,
                self._sender,
                lambda: recipient_for(row.channel, fields, self._field_cipher()),
                now=utc_now(),
                settings=self._settings,
                fields=fields,
            )
        except DispatchRefusedError as exc:
            if exc.code in SUPPRESSING_REFUSALS:
                self._suppress(row, f"{exc.code}: {exc}")
                return TickResult(claimed=True, suppressed=True, refusal=exc.code)
            # Transient: leave the row exactly as the policy left it and try again
            # next tick. ``NOT_YET_DUE`` and ``SENDER_UNAVAILABLE`` land here.
            log.info(
                "notification_scheduler_deferred",
                notification_id=row.id,
                tenant_id=str(tenant_id),
                code=exc.code,
                scheduled_at=row.scheduled_at,
            )
            return TickResult(claimed=True, refusal=exc.code)

        if outcome.deferred_until is not None:
            log.info(
                "notification_scheduler_deferred_for_quiet_hours",
                notification_id=row.id,
                tenant_id=str(tenant_id),
                resumes_at=outcome.deferred_until,
            )
            return TickResult(claimed=True, deferred=True)

        if outcome.error is not None:
            # The row already carries the attempt count and the new status; logging
            # the error text is safe because the policy truncated it in place.
            log.warning(
                "notification_scheduler_send_failed",
                notification_id=row.id,
                tenant_id=str(tenant_id),
                attempts=row.attempts,
                status=row.status.value,
            )
            return TickResult(claimed=True, refusal=None)

        log.info(
            "notification_scheduler_sent",
            notification_id=row.id,
            tenant_id=str(tenant_id),
            channel=row.channel.value,
            template=row.template,
        )
        return TickResult(claimed=True, sent=True)

    @staticmethod
    def _suppress(row: Notification, reason: str) -> None:
        """Park the row for an operator, keeping the verdict on the row itself."""
        row.status = NotificationStatus.SUPPRESSED
        row.last_error = reason[:MAX_ROW_ERROR_CHARS]
        log.info(
            "notification_scheduler_suppressed",
            notification_id=row.id,
            tenant_id=str(row.tenant_id),
            reason=row.last_error,
        )


def main() -> int:
    """``python -m app.workers.notification_scheduler``."""
    settings = get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.json_logs)

    engine = build_engine(settings)
    factory = build_session_factory(engine)

    sender = build_sender(settings)
    log.info(
        "notification_scheduler_transport",
        transport=settings.notification_transport,
        sender=type(sender).__name__,
    )

    scheduler = NotificationScheduler(factory, sender, settings=settings)
    stop = threading.Event()

    def _request_stop(signum: int, _frame: FrameType | None) -> None:
        log.info("notification_scheduler_stopping", signal=signum)
        stop.set()

    # Graceful shutdown: finish the row in flight, then exit.
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    try:
        # The clock sends most notifications, not a client, so ``notifications_sent``
        # is written here far more often than in the API process. Serving this
        # process's registry is what makes that half of the series visible.
        with publish_metrics(settings, worker="notification_scheduler"):
            scheduler.run_forever(stop)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
