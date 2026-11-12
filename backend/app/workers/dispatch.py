"""Notification dispatch policy (F10).

F9 turned an approved field into a *decision* — which channel, and why. Nothing
sent it, so ``Notification`` (in the schema since F2) still had no producer and
the pipeline stopped one step short of the customer. This module is that step, and
it is a **policy** module in the same sense as :mod:`app.workers.decision` and
:mod:`app.workers.extraction`: the rules live here, the transport lives behind a
protocol, and the route only composes the two.

Four properties this module is built around, each traced to a rule already in the
repo rather than invented here:

1. **It reuses the F9 gates instead of restating them.** ``CONTRACT_CLOSED``,
   ``CONTACT_REFUSED``, ``NO_CONSENT``, ``PIPELINE_BLOCKED`` and the "which fields
   are usable" rule all come from :mod:`app.workers.decision`. A second copy is
   how two policies start disagreeing about whether a customer may be contacted.
2. **No send that bypasses consent, opt-out or quiet hours.** Consent is re-read
   *at send time*, not trusted from the decision row: a decision is history, and a
   customer who withdrew consent an hour ago must not be emailed by a decision made
   yesterday. Quiet hours are checked immediately before the transport is reached
   and *defer* the row rather than refusing it.
3. **Quiet hours are the customer's, not the server's.** The window is evaluated
   in ``Contract.timezone`` against the ``tzdata`` IANA database, because 08:00 has
   to mean 08:00 where the customer is. An unresolvable zone is refused, never
   silently treated as UTC.
4. **Nothing reaches a caller without a transport that exists.** Every member of
   ``notification_transport`` names an adapter that is really there: ``none`` wires
   the refusing stand-in, and ``smtp`` — added with
   :class:`~app.workers.senders.SmtpSender`, not before it — wires the only
   transport this build implements. A dispatch against ``none`` records the
   attempt and fails visibly, which is the same choice ``UnavailablePublisher``
   makes for events.

What is deliberately **not** here yet, so it is not mistaken for done:

* **No SMS and no voice.** ``SmtpSender`` delivers ``email`` only, and refuses the
  other two channels rather than substituting one for another. There is no
  dialler and no gateway, so a customer who is only reachable by phone still
  cannot be reached by this build.
* **No transport code in this module.** The concrete adapter lives in
  :mod:`app.workers.senders`, as ``KafkaPublisher`` lives in ``publishers``. This
  module must stay importable, and testable, without ``smtplib`` or a relay.
* **The row lock is the caller's, not this module's.** ``Notification`` carries a
  ``version`` column (gap 21) and the callers take a ``SELECT ... FOR UPDATE`` on
  the row *before* calling :func:`dispatch_notification` — the route through
  :meth:`~app.db.repositories.notifications.NotificationRepository.claim_for_dispatch`,
  the scheduler through :func:`~app.db.repositories.notifications.claim_next_due`.
  That ordering is the enforcement: the second worker blocks, then reads the row
  already ``sent`` and refuses with ``NOT_PENDING`` instead of sending twice. The
  lock is a no-op on SQLite, so the serialisation is proven on MySQL only — gap 23.
* **No content rendering.** ``template`` names the milestone; composing the email
  or script body is the transport's job until there is a template table.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import Settings
from app.core.crypto import FieldCipher
from app.core.metrics import NOTIFICATIONS_SENT_TOTAL
from app.db.enums import Channel, NotificationStatus
from app.db.models import Contract, ExtractedField, Notification
from app.workers.decision import (
    CHANNEL_CONTACT_FIELDS,
    CLOSED_CONTRACT_STATUSES,
    REFUSAL_CONTACT_REFUSED,
    REFUSAL_CONTRACT_CLOSED,
    REFUSAL_NO_CONSENT,
    REFUSAL_PIPELINE_BLOCKED,
    USABLE_FIELD_STATUSES,
    DecisionRefusedError,
)

__all__ = [
    "DEFAULT_MILESTONE_DAYS",
    "DISPATCH_POLICY_VERSION",
    "SUPPRESSING_REFUSALS",
    "DispatchOutcome",
    "DispatchRefusedError",
    "MessageSender",
    "OutgoingMessage",
    "SenderError",
    "SenderUnavailableError",
    "UnavailableSender",
    "assert_may_contact",
    "build_notification",
    "dispatch_moment",
    "dispatch_notification",
    "in_quiet_hours",
    "milestone_for",
    "notification_idempotency_key",
    "quiet_hours_until",
    "recipient_for",
]

#: Reminder milestones, loosest first (the documented T-60 / T-30 / T-14 cadence).
#: The order matters: :func:`milestone_for` picks the *last* one that is already
#: due, so a contract inside its final two weeks gets ``T-14`` rather than a late
#: ``T-60``. The same order is why the tuple is not sorted at runtime — a reader
#: should be able to check it by eye.
DEFAULT_MILESTONE_DAYS: tuple[tuple[str, int], ...] = (("T-60", 60), ("T-30", 30), ("T-14", 14))

#: Recorded on the row's own terms. There is no ``prompt_version`` column on
#: ``Notification``, so this is documentation of which rules produced the row
#: rather than a stored field; it exists so a change to the cadence or the window
#: has a name to cite in a changelog.
DISPATCH_POLICY_VERSION = "policy.dispatch.1"

# --- machine-readable refusal reasons ---------------------------------------
#: The row is not in a state a dispatch may act on (already sent, suppressed...).
REFUSAL_NOT_PENDING = "NOT_PENDING"
#: ``scheduled_at`` is still in the future — quiet hours, or a deliberate deferral.
REFUSAL_NOT_YET_DUE = "NOT_YET_DUE"
#: The contract is further than the loosest milestone from expiry, or has no end date.
REFUSAL_NOT_IN_WINDOW = "NOT_IN_WINDOW"
REFUSAL_MISSING_END_DATE = "MISSING_END_DATE"
#: ``Contract.timezone`` is not a zone this deployment can resolve.
REFUSAL_UNKNOWN_TIMEZONE = "UNKNOWN_TIMEZONE"
#: The decision was flagged for human review, so nothing may be dispatched from it.
REFUSAL_NEEDS_REVIEW = "NEEDS_REVIEW"
#: The decision's channel is no longer consented — consent was withdrawn since.
REFUSAL_CHANNEL_NOT_CONSENTED = "CHANNEL_NOT_CONSENTED"
#: The retry budget for this row is spent; it is parked ``failed``.
REFUSAL_ATTEMPTS_EXHAUSTED = "ATTEMPTS_EXHAUSTED"
#: No transport is configured, so the attempt cannot leave the process.
REFUSAL_SENDER_UNAVAILABLE = "SENDER_UNAVAILABLE"
#: The approved contact value could not be decrypted, so there is nothing to send to.
REFUSAL_RECIPIENT_UNAVAILABLE = "RECIPIENT_UNAVAILABLE"

#: Refusals that will **not** clear on their own, so a background caller should
#: park the row rather than retry it on the next tick (gap 22).
#:
#: The distinction is not cosmetic — an unattended scheduler has to tell "wait"
#: from "stop", and getting it wrong fails in one of two ways. Retrying a
#: permanent refusal forever burns a query per row per tick and, because the due
#: queue is ordered by ``scheduled_at``, leaves the poisoned row at the head
#: starving every row behind it. Suppressing a *transient* refusal instead loses
#: the send permanently, which is worse.
#:
#: Every code here is a state a person has to change: the contract closed or was
#: opted out, consent was never given or was withdrawn, a reviewer flagged the
#: field or the decision, the contract has no end date, the time zone cannot be
#: resolved, or the approved contact value is missing. Each is recoverable — and
#: the recovery route is ``POST /notifications/{id}/retry``, which is why it
#: accepts ``suppressed`` and not only ``failed``.
#:
#: Deliberately **absent**:
#:
#: * ``NOT_YET_DUE`` — time alone resolves it. This is the deferral path.
#: * ``SENDER_UNAVAILABLE`` — a transport may be configured later. Suppressing it
#:   would be actively destructive here: the shipped transport is ``none``, so the
#:   first tick of a running scheduler would move every pending row into a state
#:   that only a human can lift, for a reason an operator fixes with one env var.
#: * ``ATTEMPTS_EXHAUSTED`` — the policy already parks that row ``failed``, and a
#:   second parking would overwrite the retry evidence with a policy verdict.
#: * ``NOT_PENDING`` — the row is not this caller's to change.
SUPPRESSING_REFUSALS: frozenset[str] = frozenset(
    {
        REFUSAL_CONTRACT_CLOSED,
        REFUSAL_CONTACT_REFUSED,
        REFUSAL_NO_CONSENT,
        REFUSAL_CHANNEL_NOT_CONSENTED,
        REFUSAL_PIPELINE_BLOCKED,
        REFUSAL_NEEDS_REVIEW,
        REFUSAL_MISSING_END_DATE,
        REFUSAL_UNKNOWN_TIMEZONE,
        REFUSAL_RECIPIENT_UNAVAILABLE,
        # Unreachable from ``dispatch_notification`` today — the milestone is
        # computed at scheduling time, not send time — but listed so a future
        # caller that does check the window does not spin on it.
        REFUSAL_NOT_IN_WINDOW,
    }
)


class DispatchRefusedError(DecisionRefusedError):
    """A send may not happen, and here is why.

    Subclasses :class:`~app.workers.decision.DecisionRefusedError` on purpose: the
    two are one policy surface with one shape — a stable ``code``, a prose message,
    and ``details`` that never contain a field value — so a route maps both to 409
    with the same helper instead of growing a second error vocabulary. ``details``
    is inherited unchanged.
    """


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """What one dispatch attempt actually did.

    Returned rather than inferred from the row, because "deferred" and "failed"
    both leave ``status == pending`` and a caller (and a test) needs to tell them
    apart without reading prose out of ``last_error``.
    """

    #: ``True`` only when a transport accepted the message.
    sent: bool
    #: Set when quiet hours moved the row forward instead of sending it. The same
    #: moment is stored in ``Notification.scheduled_at``.
    deferred_until: datetime | None = None
    #: Set when the attempt reached a transport and the transport failed.
    error: str | None = None


class SenderError(Exception):
    """A transport was reached and refused or failed the message."""


class SenderUnavailableError(SenderError):
    """No transport is configured, so the attempt never left the process."""


@dataclass(frozen=True, slots=True)
class OutgoingMessage:
    """One message handed to a transport.

    ``recipient`` is the approved contact value — the only place in this module
    where PII exists, and the reason it is ``repr=False``: an exception traceback,
    a debug log or a pytest failure that echoes the dataclass must not print an
    email address or a phone number into a log sink.

    It is decrypted from ``ExtractedField.normalized_value``, which is the column
    F9 already used to decide reachability, rather than from ``raw_value``. Reading
    the same column the decision rested on is what makes "the value we sent to" and
    "the value we chose on" one fact. That column is ciphertext (gap 20, D10-11), so
    this value exists in the clear only between :func:`recipient_for` and the
    transport — which is why it is ``repr=False``.
    """

    channel: Channel
    #: Reminder milestone (``T-60``/``T-30``/``T-14``), not a rendered body.
    template: str
    contract_id: int
    #: Passed through so a real provider can dedupe duplicate deliveries itself.
    idempotency_key: str
    recipient: str = field(repr=False)


class MessageSender(Protocol):
    """A transport that can deliver one message.

    ``channels`` exists so the policy can refuse *before* the recipient is read.
    Without it a dispatch against an unconfigured transport would still pull the
    contact value into memory to build a message it then refuses to send, and
    "read only what you are about to send" is cheaper to hold than to audit.

    It is a **set of channels rather than one boolean** because a deployment can
    legitimately have e-mail configured and SMS not. A single flag would answer
    "a transport exists" while the row's own channel could not be delivered, and
    the refusal would arrive one step too late — after the phone number had been
    decrypted to build a message that was never going to leave.
    """

    @property
    def channels(self) -> frozenset[Channel]:
        """The channels this transport can deliver on this deployment."""
        ...

    def send(self, message: OutgoingMessage) -> str | None:
        """Deliver one message, returning the provider's id when it has one.

        Returning normally means *accepted by the provider*, mirroring the outbox
        relay's rule that a normal return is an acknowledgement.
        """
        ...


class UnavailableSender:
    """The stand-in wired when ``notification_transport`` is ``none``.

    It refuses instead of pretending. A silent no-op sender would let a dispatch
    mark a row ``sent`` with nothing delivered — the failure mode this whole
    module is arranged to make impossible.

    ``reason`` names the setting that produced this stand-in, so the 409 or the
    stored ``last_error`` tells an operator which value to change instead of
    restating that nothing is configured.
    """

    def __init__(self, reason: str = "no notification transport is configured") -> None:
        self._reason = reason

    @property
    def channels(self) -> frozenset[Channel]:
        """Empty: there is nothing this stand-in could ever deliver on."""
        return frozenset()

    def send(self, message: OutgoingMessage) -> str | None:
        raise SenderUnavailableError(self._reason)


# --- quiet hours ------------------------------------------------------------


def _zone(name: str) -> ZoneInfo:
    """Resolve ``Contract.timezone``, refusing rather than assuming UTC.

    Treating an unknown zone as UTC would shift the quiet window by up to fourteen
    hours for some customers and, on the wrong edge, authorise a call in the middle
    of their night. A refusal is recoverable; a 03:00 phone call is not.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise DispatchRefusedError(
            REFUSAL_UNKNOWN_TIMEZONE,
            f"The contract's time zone {name!r} cannot be resolved, so quiet hours "
            "cannot be evaluated",
        ) from exc


def _as_aware_utc(moment: datetime) -> datetime:
    """Treat a naive datetime as UTC — the repo's storage convention — and normalise."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _naive_utc(moment: datetime) -> datetime:
    """The storage form of an instant: naive UTC, matching every other datetime here."""
    return _as_aware_utc(moment).replace(tzinfo=None)


def in_quiet_hours(local_hour: int, *, start: int, end: int) -> bool:
    """Whether ``local_hour`` falls in the half-open window ``[start, end)``.

    ``start > end`` is the normal shape (20 -> 8) and wraps midnight. ``start ==
    end`` is unreachable: ``Settings`` refuses that pair at start-up because it
    would read as "configured" while switching the rule off.
    """
    if start < end:
        return start <= local_hour < end
    return local_hour >= start or local_hour < end


def quiet_hours_until(
    contract: Contract, now: datetime, *, start: int, end: int
) -> datetime | None:
    """The UTC instant at which a quiet window opens, or ``None`` if it is open now.

    "Opens" means the first local moment at or after ``end`` o'clock at which a
    send is permitted. The arithmetic is done on **local wall clock** — "tomorrow
    at 08:00 where the customer is" — and only converted to UTC at the end, so a
    DST transition moves the instant rather than the hour the customer is called.
    """
    local = _as_aware_utc(now).astimezone(_zone(contract.timezone))
    if not in_quiet_hours(local.hour, start=start, end=end):
        return None

    opens = local.replace(hour=end, minute=0, second=0, microsecond=0)
    if opens <= local:
        opens += timedelta(days=1)
    return _naive_utc(opens)


def dispatch_moment(contract: Contract, now: datetime, *, start: int, end: int) -> datetime:
    """When this contract may next be contacted: now, or the window opening."""
    opens = quiet_hours_until(contract, now, start=start, end=end)
    return _naive_utc(_as_aware_utc(now)) if opens is None else opens


# --- milestones -------------------------------------------------------------


def milestone_for(*, end_date: date, today: date) -> tuple[str, date]:
    """The reminder milestone due for ``today``, and the date it belongs to.

    Deterministic from two dates alone, with no consultation of existing rows: the
    *caller* owns "has this milestone already been scheduled", and the unique
    ``(contract_id, template, scheduled_date)`` constraint is what enforces it. A
    function that peeked at the table would have to be given a session, and the
    policy layer deliberately does not take one.
    """
    due = [
        (template, days)
        for template, days in DEFAULT_MILESTONE_DAYS
        if today >= end_date - timedelta(days=days)
    ]
    if not due:
        loosest = max(days for _template, days in DEFAULT_MILESTONE_DAYS)
        raise DispatchRefusedError(
            REFUSAL_NOT_IN_WINDOW,
            f"The contract expires in more than {loosest} days, so no reminder "
            "milestone is due yet",
            details={"end_date": end_date.isoformat(), "today": today.isoformat()},
        )

    template, days = due[-1]
    return template, end_date - timedelta(days=days)


def notification_idempotency_key(contract_id: int, template: str, scheduled_date: date) -> str:
    """The key that makes a second send of the same slot impossible.

    Shape ``<contract>:<template>:<date>`` matches the Phase-0 fixture
    (``1042:T-30:2025-07-08``) so the demo UI and this backend agree on what a
    duplicate is. Derived only from the slot — never from a timestamp — because a
    key that embeds "when the row was written" cannot collide, and a key that
    cannot collide is not an idempotency guard.
    """
    return f"{contract_id}:{template}:{scheduled_date.isoformat()}"


# --- the contact gate -------------------------------------------------------


def assert_may_contact(contract: Contract, *, channel: Channel) -> None:
    """The consent/opt-out/closure gates, in the order F9 established.

    Extracted so :func:`build_notification` (may we *plan* a send?) and
    :func:`dispatch_notification` (may we still *make* it?) run the identical
    checks. Two call sites and one definition is the point: consent can be
    withdrawn between the two, and only a shared gate guarantees the second call
    cannot be weaker than the first.
    """
    if contract.status in CLOSED_CONTRACT_STATUSES:
        raise DispatchRefusedError(
            REFUSAL_CONTRACT_CLOSED,
            f"This contract is {contract.status.value}; no reminder is dispatched "
            "for a closed contract",
        )

    if contract.do_not_contact:
        raise DispatchRefusedError(
            REFUSAL_CONTACT_REFUSED,
            "The customer has asked not to be contacted",
        )

    consented = contract.consent_channels
    if not consented:
        raise DispatchRefusedError(
            REFUSAL_NO_CONSENT,
            "No channel consent has been recorded for this contract",
        )

    if channel not in consented:
        raise DispatchRefusedError(
            REFUSAL_CHANNEL_NOT_CONSENTED,
            f"Consent for {channel.value} is not on record for this contract",
            details={"channel": channel.value, "consented": sorted(m.value for m in consented)},
        )


def recipient_for(channel: Channel, fields: Sequence[ExtractedField], cipher: FieldCipher) -> str:
    """The approved value this channel would contact, or raise.

    Deliberately the same rule F9 used to choose the channel (``USABLE_FIELD_STATUSES``
    plus a present ``normalized_value``): if this function could accept a field the
    decision rejected, the send would go somewhere the decision never approved. That
    presence test still reads the column directly — "is there a value?" is a
    question the ciphertext answers, and asking it without decrypting is what keeps
    the decision path out of the PII.

    Returns the *plaintext* value, decrypted here and nowhere else, so the
    recipient exists in the clear only for the span of one send.

    Raises rather than returning ``None`` so the caller in
    :func:`dispatch_notification` has one failure to map onto
    :data:`REFUSAL_RECIPIENT_UNAVAILABLE`, and so a missing value cannot be mistaken
    for an empty string address. A row written before gap 20 was closed still holds
    plaintext, which ``decrypt`` rejects as a ``DecryptionError``: that lands in the
    same refusal, so an unmigrated row refuses to send instead of sending either
    ciphertext or nothing — ``python -m app.cli reseal-fields`` is the migration.

    It lives in this policy module rather than in the route that first needed it
    because the scheduler (gap 22) resolves a recipient with no HTTP request in
    sight. Two copies of this rule is how a background send starts disagreeing with
    the API about who is contactable.
    """
    wanted = CHANNEL_CONTACT_FIELDS[channel]
    for candidate in fields:
        if (
            candidate.field_name is wanted
            and candidate.status in USABLE_FIELD_STATUSES
            and candidate.normalized_value
        ):
            return cipher.decrypt(candidate.normalized_value)
    raise LookupError(f"no usable {wanted.value} value for channel {channel.value}")


# --- planning ---------------------------------------------------------------


def build_notification(
    contract: Contract,
    decision: Any,
    *,
    now: datetime,
    settings: Settings,
    fields: Sequence[ExtractedField] = (),
) -> Notification:
    """Turn a stored decision into the pending row for the milestone that is due.

    ``decision`` is typed ``Any`` deliberately rather than ``AgentDecision``: the
    policy only reads ``channel`` and ``needs_review``, and naming the ORM class
    here would invite the next reader to also read ``reasoning`` or re-derive the
    channel — which is exactly the coupling F9 drew a line against.

    The row is returned, not persisted: the caller's session owns the write, the
    same way :func:`~app.workers.extraction.extract_contract` returns rows it has
    staged rather than committing them.
    """
    if getattr(decision, "needs_review", False):
        raise DispatchRefusedError(
            REFUSAL_NEEDS_REVIEW,
            "The agent decision is flagged for human review, so nothing is dispatched from it",
        )

    channel = decision.channel
    assert_may_contact(contract, channel=channel)

    blocking = [field for field in fields if field.blocks_pipeline]
    if blocking:
        raise DispatchRefusedError(
            REFUSAL_PIPELINE_BLOCKED,
            "The pipeline is blocked by extracted fields that a person has not approved",
            details={
                "blocking": len(blocking),
                "fields": sorted({field.field_name.value for field in blocking}),
            },
        )

    if contract.end_date is None:
        raise DispatchRefusedError(
            REFUSAL_MISSING_END_DATE,
            "The contract has no end date, so no reminder milestone can be computed",
        )

    # "Today" is the customer's calendar day, not the server's: a contract one day
    # from expiry in Dhaka is not one day from expiry in UTC at 23:00.
    local_today = _as_aware_utc(now).astimezone(_zone(contract.timezone)).date()
    template, scheduled_date = milestone_for(end_date=contract.end_date, today=local_today)

    return Notification(
        tenant_id=contract.tenant_id,
        contract_id=contract.id,
        channel=channel,
        template=template,
        status=NotificationStatus.PENDING,
        scheduled_date=scheduled_date,
        scheduled_at=dispatch_moment(
            contract, now, start=settings.quiet_hours_start, end=settings.quiet_hours_end
        ),
        idempotency_key=notification_idempotency_key(contract.id, template, scheduled_date),
        attempts=0,
    )


# --- sending ----------------------------------------------------------------


def dispatch_notification(
    notification: Notification,
    contract: Contract,
    sender: MessageSender,
    resolve_recipient: Callable[[], str],
    *,
    now: datetime,
    settings: Settings,
    fields: Sequence[ExtractedField] = (),
) -> DispatchOutcome:
    """Attempt one send, mutating ``notification`` to reflect what happened.

    The gates run in a fixed order, and the order is part of the contract with the
    caller because it decides which refusal a row with several problems is told
    about — the same reasoning F9 documented for ``select_channel``:

    1. **Row state** — only a ``pending`` row is attempted.
    2. **Contact policy** — closure, ``do_not_contact``, consent for this channel,
       re-read from the contract and *not* trusted from the decision row.
    3. **Pipeline** — a field rejected since the decision blocks the send.
    4. **Retry budget** — a spent budget parks the row rather than looping.
    5. **Due time** — a future ``scheduled_at`` is ``NOT_YET_DUE``, not a send.
    6. **Quiet hours** — defers by moving ``scheduled_at``; never refuses.
    7. **Transport availability** — checked before the recipient is read.
    8. **Recipient** — read only once a send is genuinely about to happen.
    9. **Send** — a transport failure is recorded, not raised past the caller.

    ``resolve_recipient`` is a callable rather than a value so steps 1-7 can refuse
    without plaintext PII ever entering this frame.
    """
    if notification.status is not NotificationStatus.PENDING:
        raise DispatchRefusedError(
            REFUSAL_NOT_PENDING,
            f"This notification is {notification.status.value}; only a pending "
            "notification is dispatched",
            details={"status": notification.status.value},
        )

    assert_may_contact(contract, channel=notification.channel)

    blocking = [field for field in fields if field.blocks_pipeline]
    if blocking:
        raise DispatchRefusedError(
            REFUSAL_PIPELINE_BLOCKED,
            "The pipeline is blocked by extracted fields that a person has not approved",
            details={
                "blocking": len(blocking),
                "fields": sorted({field.field_name.value for field in blocking}),
            },
        )

    if notification.attempts >= settings.notification_retry_max:
        notification.status = NotificationStatus.FAILED
        raise DispatchRefusedError(
            REFUSAL_ATTEMPTS_EXHAUSTED,
            "This notification has used its dispatch attempts and is parked for an "
            "operator to review",
            details={
                "attempts": notification.attempts,
                "max": settings.notification_retry_max,
            },
        )

    now_utc = _naive_utc(_as_aware_utc(now))
    if notification.scheduled_at is not None and notification.scheduled_at > now_utc:
        raise DispatchRefusedError(
            REFUSAL_NOT_YET_DUE,
            "This notification is scheduled for later and is not due yet",
            details={"scheduled_at": notification.scheduled_at.isoformat()},
        )

    opens = quiet_hours_until(
        contract, now, start=settings.quiet_hours_start, end=settings.quiet_hours_end
    )
    if opens is not None:
        # Not a refusal: the row keeps its slot and its status, and the attempt is
        # moved to the window opening. The transport is not reached, which is the
        # whole enforcement — "no contact that bypasses quiet hours".
        notification.scheduled_at = opens
        return DispatchOutcome(sent=False, deferred_until=opens)

    if notification.channel not in sender.channels:
        raise DispatchRefusedError(
            REFUSAL_SENDER_UNAVAILABLE,
            f"No transport is configured for {notification.channel.value} notifications, "
            "so the message cannot be sent",
            details={"channel": notification.channel.value},
        )

    try:
        recipient = resolve_recipient()
    except Exception as exc:
        raise DispatchRefusedError(
            REFUSAL_RECIPIENT_UNAVAILABLE,
            "The approved contact value could not be read, so there is no recipient",
        ) from exc

    message = OutgoingMessage(
        channel=notification.channel,
        template=notification.template,
        contract_id=notification.contract_id,
        idempotency_key=notification.idempotency_key,
        recipient=recipient,
    )

    notification.attempts += 1
    try:
        provider_message_id = sender.send(message)
    except SenderError as exc:
        notification.last_error = str(exc)[:500]
        notification.status = (
            NotificationStatus.FAILED
            if notification.attempts >= settings.notification_retry_max
            else NotificationStatus.PENDING
        )
        NOTIFICATIONS_SENT_TOTAL.inc(channel=notification.channel, status="failed")
        return DispatchOutcome(sent=False, error=str(exc))

    notification.status = NotificationStatus.SENT
    notification.sent_at = now_utc
    notification.provider_message_id = provider_message_id
    notification.last_error = None
    NOTIFICATIONS_SENT_TOTAL.inc(channel=notification.channel, status="sent")
    return DispatchOutcome(sent=True)
