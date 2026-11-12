"""The escalation alert: somewhere for a raised escalation to reach a person.

An escalation exists because no customer response was accepted inside the response
window. The queue renders it, the SLA clock runs against it, and until this module
the only way anyone learned it had happened was by opening a screen. A deadline
nobody is watching is not a deadline.

Three things this module deliberately is not:

* It is **not a customer contact.** The recipient is the operator who runs the
  deployment. No consent, quiet-hours, template or suppression rule applies,
  because none of them govern mailing ourselves — and the reason an escalation
  exists is precisely that the customer could not be reached. Reading the contact
  policy here would mean an opt-out also silenced the alert that a person must
  now act.
* It is **not a per-escalation message.** A scan that opens fifty escalations is
  one event an operator needs to know about, not fifty that bury each other. One
  scan, one message, however many rows it lists.
* It is **not a retried side effect.** The escalation is already committed when
  this runs, so a failed send changes nothing except a counter and a log line. The
  queue is still correct and still visible; losing the alert is the failure mode,
  and it is counted rather than hidden.

Unlike :mod:`app.workers.attention_alerts`, this needs no memory of what it has
already said: ``raised`` is a one-time transition. Each escalation is opened once
by the claim query and then never reopened (see
:func:`~app.db.repositories.escalations.claim_silent_contracts`), so a repeated
tick cannot re-announce one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from typing import Protocol
from uuid import UUID

import structlog

from app.core.config import Settings
from app.workers.senders import create_smtp_sender

log = structlog.get_logger(__name__)

#: Constant, and containing no count. A subject that restates the queue's size
#: resets any client-side grouping on every scan, and the size belongs in the
#: body where it is read in context.
SUBJECT = "Contract agent: escalations raised"


@dataclass(frozen=True, slots=True)
class RaisedEscalation:
    """One escalation this scan opened, named only by what an operator needs.

    There is no customer name, e-mail, phone, address or date of birth here, and
    there is no field one could be added to without changing this class. The alert
    is an e-mail that lands on a relay and possibly in a mailbox archive; the
    contract id is enough to open the timeline, and it is deliberately the only
    key that reaches a customer.
    """

    tenant_id: UUID
    contract_id: int
    escalation_id: int
    #: Naive-UTC, as every stored timestamp is. Rendered with an explicit "UTC"
    #: label so a deadline cannot be read as local time by whoever receives it.
    sla_deadline: datetime


class EscalationAlerter(Protocol):
    """Says, once per scan, that escalations were raised."""

    def raised(self, escalations: Sequence[RaisedEscalation]) -> None:
        """Deliver one message describing ``escalations``.

        Returning normally means the alert was accepted. Anything else is a
        failure the caller counts; the caller never lets it propagate, because
        the escalations are already committed and must not be reported as
        unwritten.
        """
        ...


class SmtpEscalationAlerter:
    """Mails the operator through the deployment's one configured relay.

    It reuses :meth:`~app.workers.senders.SmtpSender.deliver` rather than
    :meth:`~app.workers.senders.SmtpSender.send`: everything worth sharing is
    below the envelope (the verifying TLS context, the login, the bounded socket,
    an error text scrubbed of addresses), and an operator alert has its own
    subject, no contract row and no idempotency slot.
    """

    def __init__(self, settings: Settings) -> None:
        self._sender = create_smtp_sender(settings)
        self._recipient = settings.escalation_alert_to
        self._from = settings.smtp_from

    def raised(self, escalations: Sequence[RaisedEscalation]) -> None:
        if not escalations:
            return
        self._sender.deliver(_compose(escalations, sender=self._from, recipient=self._recipient))


def build_escalation_alerter(settings: Settings) -> EscalationAlerter | None:
    """Return the alerter, or ``None`` when no address is configured.

    ``None`` is the honest default: it makes "nobody is being told" a value the
    scanner can see, render and log, instead of a silent no-op that looks wired
    up. Settings validation already guarantees that a non-empty address implies an
    SMTP transport, so this function has no second thing to check.
    """
    if not settings.escalation_alert_to:
        return None
    return SmtpEscalationAlerter(settings)


def _compose(
    escalations: Sequence[RaisedEscalation],
    *,
    sender: str,
    recipient: str,
) -> EmailMessage:
    """Build the message. Separated so the text is testable without a relay."""
    composed = EmailMessage()
    composed["Subject"] = SUBJECT
    composed["From"] = sender
    composed["To"] = recipient
    composed.set_content(_render(escalations))
    return composed


def _render(escalations: Sequence[RaisedEscalation]) -> str:
    """The body: a count, one line per escalation, then what to do next."""
    lines = [
        "An escalation was raised.",
        "",
        "No customer response was accepted inside the response window, so these",
        "contracts have been handed to a person. Nothing is sent to the customer",
        "from here: the escalation queue is the next step, and any further contact",
        "still has to pass the deterministic contact policy.",
        "",
        f"{len(escalations)} raised:",
        "",
    ]
    for item in escalations:
        lines.append(
            f"  escalation {item.escalation_id}  contract {item.contract_id}  "
            f"tenant {item.tenant_id}  SLA {item.sla_deadline.isoformat()} (UTC)"
        )
    lines.extend(
        [
            "",
            "One message per scan, never one per escalation.",
            "",
            "This address is the deployment's operator, not the customer. Nothing",
            "above is a customer contact value.",
        ]
    )
    return "\n".join(lines) + "\n"


__all__ = [
    "SUBJECT",
    "EscalationAlerter",
    "RaisedEscalation",
    "SmtpEscalationAlerter",
    "build_escalation_alerter",
]
