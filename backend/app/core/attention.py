"""The attention check: the states that need a human, and the one that only looks like one.

``python -m app.cli doctor`` counts the states that need a person while raising no
alert of their own: an outbox event parked in the dead-letter state, a notification
that ended ``failed`` or ``suppressed``, an extracted-field value still stored in
the clear from before gap 20, and an escalation whose SLA deadline has passed with
nobody working it. They share one property — a state that needs somebody and is
invisible until somebody goes looking — so one read-only reading answers all four.

The first three are failures: something that was supposed to reach a customer
stopped short, or a value that should have been sealed was not. The fourth is not a
failure at all — the notice went out, the customer went quiet, the escalation was
raised, and then nobody picked it up. It is here for the same reason and by the same
test: a deadline that passes unwatched is a promise the system made and then forgot,
and the queue screen only tells the reader who thought to open it
(``DECISIONS.md`` D10-99).

The numbers live here rather than in the command that first needed them because
there are now **two** callers with different jobs: the CLI, which turns the reading
into an exit code for a cron entry or a CI step, and
:class:`~app.workers.attention_watch.AttentionWatch`, which turns the same reading
into a log line on a schedule. A second copy of the counting logic would be a second
answer to "what needs a human", and the whole point of this check is that there is
one.

Three things this module decides, each of which could be decided wrongly without
failing a test:

**A parked event is not always a fault.** With ``EVENT_TRANSPORT=none`` — the
default — :func:`~app.workers.publishers.build_publisher` returns the refusing
stand-in, so the relay parks *every* event it claims. On a fresh installation that
is the designed behaviour, not a defect, and a check that called it one would cry
wolf on the first ``docker compose up`` and be muted shortly after — which is how a
real alarm stops being read. So a park is **explained** when the bus is switched
off, and it is still *counted*: the operator reading the line sees the number, and
the line says the events will not drain until a transport is configured. What
changes is the verdict, not the count. Everything else — a failed or suppressed
notification, a plaintext field value — needs a human whatever the transport is
configured to.

The honest limit of that distinction: it is drawn from the *current* configuration,
so events parked while a transport was live and then switched off read as explained.
The count is still on the line, and an operator who did that knows they did it. A
sharper distinction would classify each row by the error the relay recorded on it,
which needs a query that reads row text and is not worth it for a state this rare —
recorded in ``D10-39`` rather than hidden.

**No ``ENCRYPTION_KEY`` is required.** :func:`~app.core.crypto.is_sealed` recognises
a sealed token by its shape, not by decrypting it, so a run with the wrong key still
reports a plaintext database as plaintext. A check that needed the key would go
quiet at exactly the moment the key was wrong, which is the one moment it matters.

**``unsealed_field_ids`` is the only row-level detail.** No companion list of values:
an id plus a column name is enough to run ``reseal-fields --ids``, and nothing here
should be able to put PII on a log line even by accident.

**Two readings, two questions, one home.** Everything above describes the *operator*
reading: no principal, every organisation at once, four counts. When gap 22's
residue grew an HTTP route it needed a second reader, which is
:func:`tenant_attention_report` — and it answers a narrower question, *what on my own
board needs me?* It reports the counts a tenant can act on — notices they can retry,
a value they can seal, and an escalation that is late in their own queue — and
deliberately not the one they cannot: a parked outbox event is a relay fault
attributable to the deployment, so showing it to a tenant would invite a support
ticket about somebody else's misconfigured transport. The counting lives here rather
than in the route for the same reason the operator counts do — a second copy would be
a second answer to "what needs a human", and the two readings share one ``is_sealed``
scan so that they cannot eventually disagree about the same database.

**The population nothing counted (``D10-129``).** Every count above is a row that
*stopped*: ``failed``, ``suppressed``, parked. A ``pending`` row was accounted for by
the scheduler's existence, so it needed no reading — and that reasoning holds only while
something exists to claim it. With ``NOTIFICATION_TRANSPORT=none``, the shipped default,
:func:`~app.workers.dispatch.dispatch_notification` raises ``SENDER_UNAVAILABLE``
*before* ``attempts`` is incremented, so a reminder can never reach the retry cap, never
turns ``failed``, and stays ``pending`` for ever. The default install therefore published
``failed 0`` — a clean board — while a queue of customer reminders sat behind it,
unsendable and uncounted. Three counts close that, and they are separated by a single
question: **is there a setting whose change would make this channel deliverable?**

* ``unconfigured`` — the channel has a transport and it is switched off. The same shape
  as an explained park, and treated the same way: counted, published, and deliberately
  **not** a reason in :attr:`AttentionReport.needs_attention`. An install that failed
  every run after its first reminder would be muted within a week.
* ``unsupported`` — the channel is outside :data:`~app.core.config.NOTIFIABLE_CHANNELS`.
  No environment variable moves it; the human action is to change what may be consented,
  or to approve an address a channel can use. A reason. ``voice`` was its only occupant
  until ``D10-144`` gave it a dialler, so **no configuration this build ships puts
  anything here today** — the bucket is kept because the ceiling is a statement about
  the ``Channel`` enum, and a channel added to the enum before it can be sent would land
  in exactly this bucket.
* ``stuck`` — the channel *is* deliverable and the row is still overdue, so the send
  path is not running. A reason, and a fault on every configuration.

Deferral is in none of them. A row parked for quiet hours has a ``scheduled_at`` in the
future, is not yet due, and counting it would page every deployment at two in the
morning.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from app.core.config import NOTIFIABLE_CHANNELS, Settings
from app.core.crypto import is_sealed
from app.db.base import utc_now
from app.db.enums import Channel, NotificationStatus
from app.db.repositories.escalations import (
    overdue_escalation_count,
    overdue_escalation_count_for_tenant,
)
from app.db.repositories.fields import fields_for_reseal, fields_for_reseal_for_tenant
from app.db.repositories.notifications import (
    pending_overdue_counts,
    undelivered_counts,
    undelivered_counts_for_tenant,
)
from app.db.repositories.outbox import dead_letter_count

if TYPE_CHECKING:
    from app.db.models import ExtractedField

#: The two columns on :class:`~app.db.models.ExtractedField` that hold PII, and so
#: must be sealed. Named once, here, so "did we miss a column?" has one answer —
#: ``reseal-fields`` seals this set and the attention check verifies it, and a column
#: added to one without the other is the failure this constant exists to prevent.
PII_COLUMNS: tuple[str, ...] = ("raw_value", "normalized_value")


@dataclass(frozen=True, slots=True)
class AttentionReport:
    """One reading of the states that need a human.

    A record rather than the dictionary this check used to return: there are two
    callers now, and a key misspelled in one of them was a ``KeyError`` at runtime
    that no test of the other caller could catch.

    ``slots`` and ``frozen`` for the same reason every other result record in this
    codebase has them: it is a value, it is not edited after the fact, and a typo
    that assigns an attribute should raise instead of silently inventing a field.
    """

    outbox_parked: int
    notifications_failed: int
    notifications_suppressed: int
    #: Due-but-``pending`` rows past the grace on a channel whose transport is switched
    #: off. Explained by configuration, so counted and published but not a reason —
    #: the treatment an explained park already gets, and for the same reason.
    notifications_pending_unconfigured: int
    #: The same population on a channel **outside ``NOTIFIABLE_CHANNELS``**: a ceiling
    #: rather than a switch, so no environment variable clears these and a human has to
    #: act. Deliberately separate from ``unconfigured`` because the next move differs: one
    #: is a transport to configure, the other a consent or an address to change. Empty on
    #: every configuration this build ships since ``D10-144`` put ``voice`` in the ceiling.
    notifications_pending_unsupported: int
    #: The same population on a channel that *is* deliverable — so the queue is not
    #: draining and the send path is the fault. Counted apart from the other two because
    #: it is the only one of the three that is never explained by the deployment's
    #: settings.
    notifications_pending_stuck: int
    #: Escalations past their SLA deadline that nobody has resolved, table-wide.
    #: The one count here that is not a delivery failure — nothing was lost, a
    #: promise simply came due in a queue nobody was reading (D10-99).
    escalations_overdue: int
    unsealed_field_values: int
    unsealed_field_ids: tuple[int, ...]
    fields_scanned: int
    field_scan_truncated: bool
    #: The transport the check was *run* under, kept on the report so a line in a
    #: log can be read without knowing what the deployment was configured with that
    #: day. The distinction below is meaningless without it.
    event_transport: str
    bus_disabled: bool
    #: The transport the check was run under for *reminders*, kept for the reason
    #: ``event_transport`` is: ``unconfigured`` is only meaningful next to it, because
    #: the count is large on a default install by design and the reader has to be able
    #: to tell that from a broken relay without knowing the day's configuration.
    notification_transport: str

    @property
    def parks_explained(self) -> int:
        """Parked events that follow from configuration rather than from a fault.

        Zero when a transport is configured: then a parked row really is something
        the relay tried and could not deliver, and it needs a person.
        """
        return self.outbox_parked if self.bus_disabled else 0

    @property
    def parks_unexplained(self) -> int:
        """Parked events that a switched-off bus does not account for."""
        return self.outbox_parked - self.parks_explained

    @property
    def needs_attention(self) -> bool:
        """Whether a person has to do something about this reading.

        Every count except an explained park is a reason, because every one of them
        is a state that will not clear itself: a notification that stopped
        ``failed`` or ``suppressed`` sits until an operator retries or resolves it,
        a plaintext value sits until ``reseal-fields`` runs, and an overdue
        escalation sits until somebody works the row — no clock resolves it, because
        the whole reason it is there is that the contract went quiet.

        An explained park is deliberately **not** a reason, and this is the one
        judgement in the module worth arguing about. The alternative — count it and
        exit non-zero anyway — is what the check already did, and on the default
        configuration it means every run after the first reminder is a failure,
        which trains the operator to ignore the one command meant to be trusted.
        The count stays in :attr:`outbox_parked` and in every log line, so nothing
        is hidden; only the verdict changes.

        The stuck-queue counts follow that judgement rather than being carved out of
        it. :attr:`notifications_pending_unconfigured` is a switch somebody has not
        turned on — counted, published, and not a reason, exactly as an explained park
        is, and for exactly the reason above: on the default install it is non-zero the
        moment the first reminder is produced, so failing on it would make the verdict
        meaningless. :attr:`notifications_pending_unsupported` and
        :attr:`notifications_pending_stuck` are the opposite: no setting explains
        either, the first because the channel sits outside ``NOTIFIABLE_CHANNELS`` — a
        ceiling, and empty on every configuration this build ships since ``D10-144`` —
        and the second because the channel *can* be sent and is not being, so both mean
        a person has to decide something.
        """
        return bool(
            self.parks_unexplained
            or self.notifications_failed
            or self.notifications_suppressed
            or self.notifications_pending_unsupported
            or self.notifications_pending_stuck
            or self.escalations_overdue
            or self.unsealed_field_values
        )

    def as_dict(self) -> dict[str, Any]:
        """The log/JSON shape, with the key names both callers already emit.

        Flat and JSON-safe on purpose: this is what lands in a structured log line,
        and a nested object is one more thing for a log search to get wrong.
        """
        return {
            "outbox_parked": self.outbox_parked,
            "outbox_parked_explained": self.parks_explained,
            "notifications_failed": self.notifications_failed,
            "notifications_suppressed": self.notifications_suppressed,
            "notifications_pending_unconfigured": self.notifications_pending_unconfigured,
            "notifications_pending_unsupported": self.notifications_pending_unsupported,
            "notifications_pending_stuck": self.notifications_pending_stuck,
            "escalations_overdue": self.escalations_overdue,
            "unsealed_field_values": self.unsealed_field_values,
            "unsealed_field_ids": list(self.unsealed_field_ids),
            "fields_scanned": self.fields_scanned,
            "field_scan_truncated": self.field_scan_truncated,
            "event_transport": self.event_transport,
            "bus_disabled": self.bus_disabled,
            "notification_transport": self.notification_transport,
            "needs_attention": self.needs_attention,
        }


def _pending_by_cause(
    pending: dict[Channel, int],
    *,
    deliverable: frozenset[Channel],
) -> tuple[int, int, int]:
    """Split overdue-``pending`` counts into the three causes the report names.

    The single question that separates them is *is there a setting whose change would
    make this channel deliverable?* — and it is asked of the same
    :attr:`~app.core.config.Settings.deliverable_notification_channels` that
    :func:`~app.workers.senders.build_sender` wires its senders from, so the board and
    the send path cannot disagree about whether a channel exists at all.

    Order matters, and since ``D10-144`` it is the only thing keeping ``unsupported``
    meaningful. Every member of :class:`~app.db.enums.Channel` is now in
    :data:`~app.core.config.NOTIFIABLE_CHANNELS`, so on any configuration this build
    ships the branch is unreachable and no row lands in it. It is kept rather than
    deleted because the ceiling it encodes is real and outlives the switch: a channel
    added to the enum before a transport exists would produce exactly these rows, and
    checking membership in ``deliverable`` first would then report them as something
    an operator can fix when the truth is that the build cannot send them at all.

    Returns ``(unconfigured, unsupported, stuck)``.
    """
    unconfigured = unsupported = stuck = 0
    for channel, count in pending.items():
        if not count:
            continue
        if channel not in NOTIFIABLE_CHANNELS:
            unsupported += count
        elif channel in deliverable:
            stuck += count
        else:
            unconfigured += count
    return unconfigured, unsupported, stuck


def _unsealed_ids(fields: Sequence[ExtractedField]) -> tuple[int, ...]:
    """Ids of rows holding at least one PII column still in the clear.

    Shared by both readings on purpose. ``reseal-fields`` seals :data:`PII_COLUMNS`,
    the operator check verifies it, and now a tenant's own board verifies it for that
    tenant — three consumers of one predicate. Two copies of this comprehension would
    be two answers to "is this row sealed?", and the day one copy learned about a new
    column the other would keep reporting a clean board for it.

    ``is_sealed`` is shape-based, so this needs no ``ENCRYPTION_KEY``; that property is
    claimed in the module docstring and would be lost by a helper that decrypted.
    """
    return tuple(
        field.id
        for field in fields
        if any(
            getattr(field, column) is not None and not is_sealed(str(getattr(field, column)))
            for column in PII_COLUMNS
        )
    )


def attention_report(
    session: Session,
    *,
    limit: int,
    settings: Settings,
    now: datetime | None = None,
) -> AttentionReport:
    """Read the attention states on an open session.

    Never writes and never commits: the caller owns the transaction, as everywhere
    else in this codebase.

    ``limit`` bounds the field scan. :func:`~app.db.repositories.fields.fields_for_reseal`
    stops at that many rows, so a full page makes the unsealed count a *floor*
    rather than the table's total. That is recorded as ``field_scan_truncated``
    rather than rounded away: "0 unsealed" from a truncated scan is the one answer
    this check must never report confidently.

    ``now`` decides whether an escalation is late, and defaults to the wall clock the
    way :func:`~app.db.repositories.notifications.claim_next_due` does. A test can
    pin it; nothing in production needs to, because the caller asking "what needs a
    human *now*" is the only caller there is. ``limit`` deliberately does **not**
    bound the escalation count: unlike the field scan there is no unbounded read
    behind it, only a ``COUNT`` the database answers in one statement, so truncating
    it would add a "floor, not a total" caveat to a number that does not need one.
    """
    undelivered = undelivered_counts(session)
    fields = fields_for_reseal(session, limit=limit)
    unsealed = _unsealed_ids(fields)
    pending = pending_overdue_counts(
        session,
        grace=settings.notification_overdue_grace,
        now=utc_now() if now is None else now,
    )
    unconfigured, unsupported, stuck = _pending_by_cause(
        pending, deliverable=settings.deliverable_notification_channels
    )

    return AttentionReport(
        outbox_parked=dead_letter_count(session),
        notifications_failed=undelivered[NotificationStatus.FAILED],
        notifications_suppressed=undelivered[NotificationStatus.SUPPRESSED],
        notifications_pending_unconfigured=unconfigured,
        notifications_pending_unsupported=unsupported,
        notifications_pending_stuck=stuck,
        escalations_overdue=overdue_escalation_count(
            session, now=utc_now() if now is None else now
        ),
        unsealed_field_values=len(unsealed),
        unsealed_field_ids=unsealed,
        fields_scanned=len(fields),
        field_scan_truncated=len(fields) == limit,
        event_transport=settings.event_transport,
        bus_disabled=settings.event_transport == "none",
        notification_transport=settings.notification_transport,
    )


@dataclass(frozen=True, slots=True)
class TenantAttentionReport:
    """One reading of **one organisation's** attention states.

    Deliberately not a field-for-field copy of :class:`AttentionReport`. There is no
    ``outbox_parked`` because a parked event is a deployment fault rather than a
    tenant's, and no ``event_transport``/``bus_disabled`` because whether the bus is
    switched off is neither something a tenant can act on nor something they should
    hear about from their own board. A shared record with three fields blanked for
    this reader would make "this tenant has no parks" and "we do not report parks to
    tenants" the same value — which is exactly the confusion ``D10-39`` spent a
    section avoiding on the operator side.

    ``escalations_overdue`` **is** here, and the test that admits it is the same one
    that excludes the park: the queue is theirs to work, so a late row on their board
    is a row they can clear. Counting only their own rows is what makes it
    actionable — an operator-wide number would tell a tenant that *somebody* is late
    without telling them whether it is their problem (D10-99).

    ``slots`` and ``frozen`` for the same reason every other result record here has
    them: it is a value, it is not edited after the fact, and a typo that assigns an
    attribute should raise rather than invent a field.

    **None of ``D10-129``'s three pending counts is here**, and the test is the one
    that excludes the park. Each of them is a statement about what *this deployment*
    can send: ``unconfigured`` is a transport somebody has not switched on,
    ``unsupported`` is a channel the build has no sender for, ``stuck`` is a send path
    that has stopped claiming rows. None is a row an organisation can clear from its
    own board, and telling every tenant that the scheduler is down would be one
    deployment fault arriving as N support tickets — verbatim the argument this record
    already makes for a parked event.

    The population is not hidden from them either, which is what makes the exclusion
    safe rather than merely tidy: unlike an operator, whose only reading of this table
    was a count, a tenant has the notification queue screen and its ``status`` filter,
    so every one of their ``pending`` rows is visible to them by name. A separate
    count would tell them nothing the queue does not already show.
    """

    notifications_failed: int
    notifications_suppressed: int
    escalations_overdue: int
    unsealed_field_values: int
    unsealed_field_ids: tuple[int, ...]
    fields_scanned: int
    field_scan_truncated: bool

    @property
    def needs_attention(self) -> bool:
        """Whether somebody in this organisation can act on the reading.

        Every count here is actionable by the organisation that owns the rows: a
        ``failed``/``suppressed`` notification is retried — or the policy gate that
        suppressed it is fixed — from their own queue, a plaintext field value is
        sealed from their own data, and an overdue escalation is assigned or resolved
        by whoever is on call. That is the same test that *disqualifies* the park
        count from this report: a tenant cannot fix a relay.
        """
        return bool(
            self.notifications_failed
            or self.notifications_suppressed
            or self.escalations_overdue
            or self.unsealed_field_values
        )

    def as_dict(self) -> dict[str, Any]:
        """The wire shape. Flat and JSON-safe, as the operator report's is."""
        return {
            "notifications_failed": self.notifications_failed,
            "notifications_suppressed": self.notifications_suppressed,
            "escalations_overdue": self.escalations_overdue,
            "unsealed_field_values": self.unsealed_field_values,
            "unsealed_field_ids": list(self.unsealed_field_ids),
            "fields_scanned": self.fields_scanned,
            "field_scan_truncated": self.field_scan_truncated,
            "needs_attention": self.needs_attention,
        }


def tenant_attention_report(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    limit: int,
    now: datetime | None = None,
) -> TenantAttentionReport:
    """Read one organisation's attention states on an open session.

    Never writes and never commits: the caller owns the transaction, as everywhere
    else in this codebase.

    ``tenant_id`` must come from the authenticated principal. This function cannot
    enforce that — it has no way to know who is asking — which is the same division
    every repository in this codebase uses: the data layer scopes, the route is
    responsible for having got the id from :class:`~app.core.principal.Principal`
    rather than from the request. ``GET /attention`` reads ``principal.tenant_id``
    and accepts no tenant from anywhere else.

    ``limit`` bounds the field scan exactly as :func:`attention_report` does, and a
    full page makes the unsealed count a floor rather than a total, reported as
    ``field_scan_truncated``. "0 unsealed" from a truncated scan is the one answer
    this report must never give confidently either.

    ``now`` decides whether an escalation is late, and defaults to the wall clock for
    the same reason and with the same escape hatch as :func:`attention_report`'s.
    """
    undelivered = undelivered_counts_for_tenant(session, tenant_id)
    fields = fields_for_reseal_for_tenant(session, tenant_id, limit=limit)
    unsealed = _unsealed_ids(fields)

    return TenantAttentionReport(
        notifications_failed=undelivered[NotificationStatus.FAILED],
        notifications_suppressed=undelivered[NotificationStatus.SUPPRESSED],
        escalations_overdue=overdue_escalation_count_for_tenant(
            session, tenant_id, now=utc_now() if now is None else now
        ),
        unsealed_field_values=len(unsealed),
        unsealed_field_ids=unsealed,
        fields_scanned=len(fields),
        field_scan_truncated=len(fields) == limit,
    )
