"""The operator alert: somewhere for the attention verdict to reach a person.

``D10-39`` gave the attention check a process and left its last link open on
purpose. The verdict was a log line, and nothing in this stack reads the log, so
"needs a person" still meant "needs a person who happens to look". The obvious next
step — an HTTP route serving the same report — turned out to be *blocked* rather
than merely unbuilt: :func:`~app.core.attention.attention_report` counts parked
events, undelivered notifications and unsealed fields **across tenants** on
purpose, and each of those queries says in its own docstring that scoping it to one
tenant would be actively misleading, while a route answers a *tenant* principal. An
operator's question cannot be answered with a tenant's credentials.

This module answers it without touching that problem, because the recipient is not
a tenant. It is a person on the operations side, named by
:attr:`~app.core.config.Settings.attention_alert_to`, and the message travels the
transport this build already has and has already proven against a real socket:
:class:`~app.workers.senders.SmtpSender`. No new dependency, no new column, no new
principal, no migration.

Three decisions that could each be made wrongly without failing a test:

**It alerts on a change of verdict, never on the verdict itself.** At the default
five-minute interval a standing finding is 288 messages a day, which is a mailbox
filter by Thursday — and an alarm nobody reads is the exact failure ``D10-39``
exists to prevent. So the worker remembers the last verdict it successfully told
the operator about and speaks only when the answer differs: once when a finding
appears, once when the board is clear again, nothing while it stands. The second
message is not politeness. Without it an operator cannot tell "fixed" from "never
fired", and the first thing they do is check by hand — the behaviour this exists to
remove.

**A clean board on the very first tick sends nothing.** The remembered verdict
starts calm, because nothing has ever been sent and a calm baseline is the only
thing it is reasonable for the operator to believe. A deployment that boots
*broken* therefore alerts on its first tick rather than staying quiet until
something changes: the asymmetry fails in the direction that matters.

**The state is process memory, and that limit is stated rather than hidden.** It
survives between ticks and not across a restart, so a worker that restarts while a
finding stands re-alerts once. The repair is a small table (``0007``) holding the
last notified verdict; it is not worth a schema change on the thinnest data this
system has until a restarting worker is actually observed to be noisy. The same
trade, for the same reason, as ``D10-39``'s explained/unexplained split.

What this is deliberately **not**:

* **Not a notification, and it reads none of the notification policy.** No consent
  lookup, no quiet hours, no per-tenant template. Those gates exist to protect a
  *data subject* the system decided to contact; this contacts the person who chose
  to run the system, at an address that person configured. Routing an operator
  alert through the customer-consent path would be a category error that also
  required deciding which tenant's consent to consult.
* **Not a second transport.** It reuses ``SMTP_*`` and ``NOTIFICATION_TRANSPORT``,
  so there is one relay to configure, one set of credentials to rotate and one
  socket path already covered by ``test_smtp_socket.py``.
* **Not a pager.** One address, no escalation ladder, no acknowledgement tracking.
  A deployment that wants a pager points this address at the pager's mail gateway,
  which is what mail gateways are for.
"""

from __future__ import annotations

from collections.abc import Callable
from email.message import EmailMessage
from typing import Protocol

from app.core.attention import AttentionReport
from app.core.config import Settings
from app.workers.senders import create_smtp_sender

__all__ = [
    "SUBJECT_CLEARED",
    "SUBJECT_RAISED",
    "AttentionAlerter",
    "SmtpAttentionAlerter",
    "build_attention_alerter",
]

#: Constant per direction so one mail rule can file both. No counts in the subject
#: on purpose: a rule matching "attention needed" must also match the tenth
#: recurrence, and a subject that changes with the numbers cannot be matched at all.
SUBJECT_RAISED = "Contract agent: attention needed"
SUBJECT_CLEARED = "Contract agent: attention cleared"

_RAISED_HEADLINE = (
    "The contract agent health check needs a person.\n"
    "\n"
    "These are states that raise no alarm of their own: an outbox event parked\n"
    "in the dead-letter state on a deployment that has a transport to deliver\n"
    "it, a notification that ended failed or suppressed, an extracted-field\n"
    "value still stored in the clear, or an escalation whose SLA deadline has\n"
    "passed with nobody working it. None of them gets better on its own.\n"
    "\n"
    "The last of those is not a delivery failure: the notice went out, the\n"
    "customer went quiet, and the row is then somebody's work rather than\n"
    "something that broke.\n"
)

#: Said only to a deployment that has the bus switched off *and* something parked.
#:
#: The headline above names a parked event as one of the reasons, and that sentence
#: is false for exactly one configuration: with no event transport the check treats a
#: park as explained and does not raise on it (``D10-39``), so a mail that names parks
#: beside the counts is describing a reading it is not looking at. Both conditions are
#: needed — a bus that is on makes every park a fault, and a bus-off deployment with
#: nothing parked has nothing to explain — so either one alone would add a paragraph
#: that does not apply.
_EXPLAINED_PARKS = (
    "Event delivery is switched off in this deployment, so a parked event is\n"
    "what the configuration produces rather than a fault. The parked count\n"
    "below is not one of the reasons for this message; it is shown because the\n"
    "number is still worth knowing.\n"
)

#: The one headline that makes a claim about the **whole** board rather than about the
#: findings on it: "clear again" and "what is true right now" are statements about
#: everything, and a scan that stopped on a full page has read a prefix of the table.
_CLEARED_HEADLINE = (
    "The contract agent health check is clear again.\n"
    "\n"
    "Whatever the previous message reported has been dealt with, or was\n"
    "reported in error. The reading below is what is true right now.\n"
)

#: Said only when the clearing reading came from a scan that ended on a full page.
#:
#: ``field_scan_truncated`` is ``len(fields) == limit``, so the "0 unsealed" beneath a
#: truncated scan is the absence of a finding *in the rows read*, not in the table.
#: :func:`~app.core.attention.attention_report` says in its own docstring that this is
#: the one answer the check must never report confidently — and the headline above
#: reported it confidently, because nothing compared the sentence with the flag two
#: lines under it (``D10-105``). The counts were right, the log line was right, and the
#: operator was told a page of the table was the table.
#:
#: Only in the clearing direction. The raised message's counts are floors in the
#: direction that *raises*, so a partial scan cannot make it claim something untrue;
#: this is the message that asserts nothing is left.
_PARTIAL_SCAN = (
    "The field scan below stopped at its page limit, so clear here means that no\n"
    "unsealed value turned up in the rows it read, not that none exists. Rows\n"
    "past that limit were not read, and a finding among them would go unreported\n"
    "by this check.\n"
)


def _cleared_mail_headline(report: AttentionReport) -> str:
    """The clearing prose, with the caveat when the clearing reading was a partial one.

    A separate function rather than a branch in :func:`_body` because the paragraph
    qualifies the *headline's* claim, and :func:`_body` is handed a headline it did not
    choose: putting it there would have put a caveat about "clear" underneath the raised
    message too, where it qualifies nothing.

    The flag is read from the report the alerter was handed, never from a second query,
    so the sentence and the counts under it describe the same read.
    """
    if not report.field_scan_truncated:
        return _CLEARED_HEADLINE
    return f"{_CLEARED_HEADLINE}\n{_PARTIAL_SCAN}"


_FOOTER = (
    "\n"
    "This message is sent when the verdict changes, not on every check, so it\n"
    "will not repeat while this stays the state.\n"
)


def _body(headline: str, report: AttentionReport) -> str:
    """The verdict, rendered from the same dictionary the log line carries.

    Built from :meth:`~app.core.attention.AttentionReport.as_dict` rather than from
    hand-picked fields, so the mail and the log cannot drift apart: a count added to
    the report shows up in both places or in neither. It is also why nothing here
    picks values out of the database — the report holds ids and counts only, so the
    mail cannot leak a contract value even by accident.

    One paragraph is *not* fixed prose, which is the exception ``D10-101`` added:
    the headline says a parked event is a reason, and with the bus switched off that
    sentence is false (``D10-39``). The report carries the flag, so the mail can say
    which reading it is describing instead of leaving the operator to infer it from
    ``bus_disabled: true`` underneath. Nothing else *this function composes* varies;
    the headline it is handed may, which is how the clearing message qualifies a
    partial scan (``D10-105``).
    """
    counts = "\n".join(f"{key}: {value}" for key, value in report.as_dict().items())
    explained = f"\n{_EXPLAINED_PARKS}" if report.bus_disabled and report.outbox_parked > 0 else ""
    return f"{headline}{explained}\n{counts}\n{_FOOTER}"


class AttentionAlerter(Protocol):
    """Where a change of verdict goes. There are exactly two things to say."""

    def raised(self, report: AttentionReport) -> None:
        """A finding appeared, and the operator has not been told about it."""
        ...

    def cleared(self, report: AttentionReport) -> None:
        """The board is clear again, and the operator was told it was not."""
        ...


class SmtpAttentionAlerter:
    """Composes the operator mail and hands it to the shared SMTP transport.

    It takes ``deliver`` rather than a connection — the same reason
    :class:`~app.workers.senders.SmtpSender` takes a *factory*: a socket captured in
    a long-lived worker would be a socket held open between findings, and a test of
    the message text should not need a relay at all.
    """

    def __init__(
        self,
        deliver: Callable[[EmailMessage], None],
        *,
        recipient: str,
        sender_address: str,
    ) -> None:
        self._deliver = deliver
        self._recipient = recipient
        self._sender_address = sender_address

    def raised(self, report: AttentionReport) -> None:
        self._send(SUBJECT_RAISED, _RAISED_HEADLINE, report)

    def cleared(self, report: AttentionReport) -> None:
        self._send(SUBJECT_CLEARED, _cleared_mail_headline(report), report)

    def _send(self, subject: str, headline: str, report: AttentionReport) -> None:
        composed = EmailMessage()
        composed["Subject"] = subject
        composed["From"] = self._sender_address
        composed["To"] = self._recipient
        # No ``Message-ID``, deliberately — the one place this differs from
        # ``SmtpSender._compose``. A reminder derives its id from the idempotency
        # slot so that a re-send after a crash is recognisable as a duplicate; an
        # alert has no slot and is never re-sent, because it only happens when the
        # verdict changes. Deriving an id here would make the second finding of the
        # week look like the first to any provider that de-duplicates on that
        # header, which is the one way this message could be silently dropped.
        composed.set_content(_body(headline, report))
        self._deliver(composed)


def build_attention_alerter(settings: Settings) -> AttentionAlerter | None:
    """The alerter these settings ask for, or ``None`` for log-only.

    ``None`` rather than a refusing stand-in — the opposite of
    :func:`~app.workers.senders.build_sender` — because an unset address is a
    legitimate configuration, not a misconfiguration: log-only is what this worker
    was until now, and it stays the default. Settings validation has already refused
    an address with no transport behind it, so a non-``None`` return here always has
    a way out.
    """
    if not settings.attention_alert_to:
        return None

    sender = create_smtp_sender(settings)
    return SmtpAttentionAlerter(
        sender.deliver,
        recipient=settings.attention_alert_to,
        sender_address=settings.smtp_from,
    )
