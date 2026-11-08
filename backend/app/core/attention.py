"""The attention check: the states that need a human, and the one that only looks like one.

``python -m app.cli doctor`` counts the states that need a person while raising no
alert of their own: an outbox event parked in the dead-letter state, a notification
that ended ``failed`` or ``suppressed``, and an extracted-field value still stored in
the clear from before gap 20. They are one defect wearing three hats — a state that
needs somebody and is invisible until somebody goes looking — so one read-only
reading answers all three.

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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.crypto import is_sealed
from app.db.enums import NotificationStatus
from app.db.repositories.fields import fields_for_reseal
from app.db.repositories.notifications import undelivered_counts
from app.db.repositories.outbox import dead_letter_count

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
    unsealed_field_values: int
    unsealed_field_ids: tuple[int, ...]
    fields_scanned: int
    field_scan_truncated: bool
    #: The transport the check was *run* under, kept on the report so a line in a
    #: log can be read without knowing what the deployment was configured with that
    #: day. The distinction below is meaningless without it.
    event_transport: str
    bus_disabled: bool

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
        and a plaintext value sits until ``reseal-fields`` runs.

        An explained park is deliberately **not** a reason, and this is the one
        judgement in the module worth arguing about. The alternative — count it and
        exit non-zero anyway — is what the check already did, and on the default
        configuration it means every run after the first reminder is a failure,
        which trains the operator to ignore the one command meant to be trusted.
        The count stays in :attr:`outbox_parked` and in every log line, so nothing
        is hidden; only the verdict changes.
        """
        return bool(
            self.parks_unexplained
            or self.notifications_failed
            or self.notifications_suppressed
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
            "unsealed_field_values": self.unsealed_field_values,
            "unsealed_field_ids": list(self.unsealed_field_ids),
            "fields_scanned": self.fields_scanned,
            "field_scan_truncated": self.field_scan_truncated,
            "event_transport": self.event_transport,
            "bus_disabled": self.bus_disabled,
            "needs_attention": self.needs_attention,
        }


def attention_report(
    session: Session,
    *,
    limit: int,
    settings: Settings,
) -> AttentionReport:
    """Read the attention states on an open session.

    Never writes and never commits: the caller owns the transaction, as everywhere
    else in this codebase.

    ``limit`` bounds the field scan. :func:`~app.db.repositories.fields.fields_for_reseal`
    stops at that many rows, so a full page makes the unsealed count a *floor*
    rather than the table's total. That is recorded as ``field_scan_truncated``
    rather than rounded away: "0 unsealed" from a truncated scan is the one answer
    this check must never report confidently.
    """
    undelivered = undelivered_counts(session)
    fields = fields_for_reseal(session, limit=limit)

    unsealed = tuple(
        field.id
        for field in fields
        if any(
            getattr(field, column) is not None and not is_sealed(str(getattr(field, column)))
            for column in PII_COLUMNS
        )
    )

    return AttentionReport(
        outbox_parked=dead_letter_count(session),
        notifications_failed=undelivered[NotificationStatus.FAILED],
        notifications_suppressed=undelivered[NotificationStatus.SUPPRESSED],
        unsealed_field_values=len(unsealed),
        unsealed_field_ids=unsealed,
        fields_scanned=len(fields),
        field_scan_truncated=len(fields) == limit,
        event_transport=settings.event_transport,
        bus_disabled=settings.event_transport == "none",
    )
