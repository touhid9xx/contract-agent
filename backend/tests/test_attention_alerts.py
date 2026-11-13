"""The operator alert: gap 22's last link, closed without a tenant principal.

``D10-39`` gave the attention check a process and stopped one step short on purpose —
the verdict was a log line, and nothing here reads the log. The obvious next step, an
HTTP route serving the same report, is *blocked*: the counts are read across tenants
on purpose and a route authenticates a tenant principal.
:mod:`app.workers.attention_alerts` goes around that by mailing the deployment's
*operator*, who is not a tenant, through the SMTP transport this build already has.

What is worth pinning here is not "a mail was sent". It is the four rules whose wrong
versions all look like working software:

* **A change of verdict, not the verdict.** A standing finding at the default
  interval is 288 mails a day, which is a mailbox rule by Thursday and precisely the
  alarm ``D10-39`` exists to prevent. Asserted by ticking twice against an unchanged
  finding and counting the messages.
* **A broken first tick speaks; a clean first tick does not.** The remembered verdict
  starts calm, so a deployment that boots broken alerts at once and one that boots
  clean says nothing. Both directions asserted, because inverting the comparison
  still passes either test on its own.
* **Clearing the board is announced exactly once.** Without it an operator cannot
  tell "fixed" from "never fired" and goes to check by hand — the behaviour this
  exists to remove. Once, because the second message is the noise again.
* **State advances on a successful send only.** A relay that was down has informed
  nobody, so the next tick must try again rather than record a delivery that never
  happened.

Deliberately untested here: that the relay accepts the message, the TLS context and
the socket timeout — those belong to ``test_senders.py`` and ``test_smtp_socket.py``,
and this module reuses that path rather than reimplementing it. The one thing
asserted about the reuse is that the alert is handed to ``SmtpSender.deliver`` and
not to a private copy.
"""

from __future__ import annotations

import logging
from email.message import EmailMessage
from typing import Any

import pytest
import structlog
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs

from app.core.attention import AttentionReport
from app.core.config import Settings
from app.db.enums import ContractStatus
from app.db.models import Contract, ExtractedField, Tenant
from app.db.session import session_scope
from app.workers.attention_alerts import (
    SUBJECT_CLEARED,
    SUBJECT_RAISED,
    SmtpAttentionAlerter,
    build_attention_alerter,
)
from app.workers.attention_watch import AttentionWatch
from app.workers.dispatch import SenderError
from app.workers.senders import SmtpSender

# The helpers that make a *finding* are reused rather than rebuilt: a second copy of
# "one plaintext field" would be a second thing to keep in step with the check itself.
# The ``contract`` fixture below is not imported alongside them — a fixture can only be
# shared across modules by import, and importing it here collides with the `contract`
# parameter every test in this file takes (ruff F811, once per test). The row it builds
# is the same one ``test_attention_watch`` builds, which is also what the other nine
# test modules that need a contract do; the *finding* is what must not drift, and that
# is the `add_field(..., sealed=False)` call this file shares.
from tests.test_attention_watch import PLAINTEXT_EMAIL, add_field, events

SENDER_ADDRESS = "agent@example.test"
OPERATOR_ADDRESS = "ops@example.test"


@pytest.fixture
def contract(session_factory: sessionmaker[Session], tenant: Tenant) -> Contract:
    """The row a field hangs off. Same shape as the one in ``test_attention_watch``."""
    with session_scope(session_factory) as session:
        row = Contract(
            tenant_id=tenant.id,
            customer_ref="A. Rahman",
            timezone="UTC",
            status=ContractStatus.ACTIVE,
        )
        session.add(row)
        session.flush()
        return row


@pytest.fixture(autouse=True)
def _capture_at_debug() -> None:
    """Lower the capture threshold so a clean tick's ``debug`` line is observable.

    Same reason as in ``test_attention_watch.py``: the verdict line for a healthy
    board is ``debug``, and several assertions here are about what is *absent* — which
    is only a real absence if the quiet line would otherwise have shown up.
    """
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))


class RecordingAlerter:
    """An alerter that records instead of sending, and can be told to fail.

    It satisfies :class:`~app.workers.attention_alerts.AttentionAlerter` structurally,
    so the watch cannot tell it from the SMTP one — which is the point: the transition
    logic is tested without a relay, and the message text is tested separately.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, AttentionReport]] = []
        self.fail = fail

    def raised(self, report: AttentionReport) -> None:
        self._record("raised", report)

    def cleared(self, report: AttentionReport) -> None:
        self._record("cleared", report)

    def _record(self, direction: str, report: AttentionReport) -> None:
        if self.fail:
            # The real sender's failure type, so what is under test is the watch's
            # handling rather than the double's.
            raise SenderError("relay refused")
        self.calls.append((direction, report))

    @property
    def directions(self) -> list[str]:
        return [direction for direction, _ in self.calls]

    @property
    def reports(self) -> list[AttentionReport]:
        return [report for _, report in self.calls]


def _clear_findings(session_factory: sessionmaker[Session], field_id: int) -> None:
    """Remove the finding, so the next tick reads a clean board.

    The check is read-only and there is no "acknowledge" verb, so repairing the data
    *is* how a finding goes away — which is what an operator does in real life
    (``reseal-fields``, ``outbox-replay``).
    """
    with session_scope(session_factory) as session:
        row = session.get(ExtractedField, field_id)
        assert row is not None
        session.delete(row)


def _report_for(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> AttentionReport:
    """A real finding, read by the real check — not a hand-built report.

    Built through ``AttentionWatch`` so the shape the alerter receives is the shape
    production hands it.
    """
    add_field(session_factory, contract)
    alerter = RecordingAlerter()
    AttentionWatch(session_factory, settings=settings, alerter=alerter).run_once()
    return alerter.reports[0]


def _composer() -> tuple[SmtpAttentionAlerter, list[EmailMessage]]:
    """The real composer with a recording sink where the SMTP transport would be."""
    sent: list[EmailMessage] = []
    alerter = SmtpAttentionAlerter(
        sent.append, recipient=OPERATOR_ADDRESS, sender_address=SENDER_ADDRESS
    )
    return alerter, sent


# --- a change of verdict, not the verdict ---------------------------------
def test_a_finding_on_the_first_tick_is_mailed_once(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """A deployment that boots broken must not stay silent until something moves.

    Nothing has ever been sent, so the operator's baseline is "fine" — which is
    exactly the belief that needs correcting.
    """
    add_field(session_factory, contract)
    alerter = RecordingAlerter()

    AttentionWatch(session_factory, settings=settings, alerter=alerter).run_once()

    assert alerter.directions == ["raised"]
    assert alerter.reports[0].needs_attention is True


def test_a_clean_first_tick_mails_nothing(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    """The other half of the baseline rule: a healthy install is not a finding.

    Inverting this would mail every fresh deployment "attention cleared" before it
    had ever said anything was wrong.
    """
    alerter = RecordingAlerter()

    with capture_logs() as captured:
        AttentionWatch(session_factory, settings=settings, alerter=alerter).run_once()

    assert alerter.calls == []
    assert events(captured) == ["attention_watch_clean"]


def test_a_standing_finding_is_not_mailed_again(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """288 messages a day is the failure this design exists to avoid.

    Two ticks, one unchanged finding, one message — and the second tick still logs its
    verdict, because alerting must not make the log quieter.
    """
    add_field(session_factory, contract)
    alerter = RecordingAlerter()
    watch = AttentionWatch(session_factory, settings=settings, alerter=alerter)

    watch.run_once()
    with capture_logs() as captured:
        watch.run_once()

    assert alerter.directions == ["raised"]
    assert events(captured) == ["attention_watch_needs_attention"]


def test_clearing_the_board_is_announced_exactly_once(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """Whether the board is fixed or the check never fired must be distinguishable.

    Without the second message the operator's only recourse is to go and look, which
    is the manual step this slice removes. Exactly once, because a permanently clean
    board is the normal state.
    """
    field_id = add_field(session_factory, contract)
    alerter = RecordingAlerter()
    watch = AttentionWatch(session_factory, settings=settings, alerter=alerter)

    watch.run_once()
    _clear_findings(session_factory, field_id)
    watch.run_once()
    watch.run_once()

    assert alerter.directions == ["raised", "cleared"]
    assert alerter.reports[1].needs_attention is False


def test_a_clean_board_with_an_alerter_logs_no_extra_line(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    """Alerting is a side path; the one-verdict-per-tick rule survives it."""
    alerter = RecordingAlerter()

    with capture_logs() as captured:
        AttentionWatch(session_factory, settings=settings, alerter=alerter).run_once()

    assert events(captured) == ["attention_watch_clean"]


# --- a relay that is down has told nobody ---------------------------------
def test_a_failed_alert_is_retried_on_the_next_tick(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """The state advances on a *successful* send, or it records a lie.

    A worker that marked the finding "reported" while the relay refused would never
    speak again for that finding — the worst failure available to the one component
    whose entire job is to speak.
    """
    add_field(session_factory, contract)
    alerter = RecordingAlerter(fail=True)
    watch = AttentionWatch(session_factory, settings=settings, alerter=alerter)

    with capture_logs() as captured:
        watch.run_once()
    assert events(captured).count("attention_alert_failed") == 1

    with capture_logs() as captured:
        watch.run_once()
    assert events(captured).count("attention_alert_failed") == 1

    # The relay comes back: the next tick is still the *first* successful one.
    alerter.fail = False
    watch.run_once()

    assert alerter.directions == ["raised"]


def test_a_failed_alert_does_not_raise_out_of_the_tick(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """A problem with the post must not stop the watch.

    The reading already happened and its line already went out; dying here would trade
    a delivery failure for a monitoring outage.
    """
    add_field(session_factory, contract)
    watch = AttentionWatch(session_factory, settings=settings, alerter=RecordingAlerter(fail=True))

    report = watch.run_once()  # must not raise

    assert report.needs_attention is True


def test_no_alerter_means_the_alert_path_is_never_taken(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """The default deployment is log-only, and must stay that way."""
    add_field(session_factory, contract)

    with capture_logs() as captured:
        AttentionWatch(session_factory, settings=settings).run_once()

    assert events(captured) == ["attention_watch_needs_attention"]


# --- what the mail actually says ------------------------------------------
def test_the_mail_names_the_operator_and_the_sender(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """A message with the wrong ``To`` is a message to the wrong person."""
    report = _report_for(session_factory, settings, contract)
    alerter, sent = _composer()

    alerter.raised(report)
    alerter.cleared(report)

    assert sent[0]["To"] == OPERATOR_ADDRESS
    assert sent[0]["From"] == SENDER_ADDRESS
    assert sent[0]["Subject"] == SUBJECT_RAISED
    # Constant per direction so one mail rule can file both, and no counts in the
    # subject: a rule matching the tenth recurrence must match the first.
    assert sent[1]["Subject"] == SUBJECT_CLEARED


def test_the_mail_repeats_the_counts_the_log_line_carries(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """Mail and log are one reading rendered twice, so they must not drift.

    The body is built from ``AttentionReport.as_dict()``, the same dictionary the log
    event carries, so this asserts the shared source rather than a formatted copy.
    """
    report = _report_for(session_factory, settings, contract)
    alerter, sent = _composer()

    alerter.raised(report)
    body = sent[0].get_content()

    for key, value in report.as_dict().items():
        assert f"{key}: {value}" in body


def test_the_mail_carries_no_stored_customer_value(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """The finding is *about* a plaintext value; the mail must not be the second leak.

    The same rule as the log line, for a stricter audience: an operator's mailbox is
    copied, forwarded and retained for years, and the report holds ids and counts for
    exactly this reason.
    """
    report = _report_for(session_factory, settings, contract)
    alerter, sent = _composer()

    alerter.raised(report)
    body = sent[0].get_content()

    assert PLAINTEXT_EMAIL not in body
    assert "unsealed_field_ids" in body  # the actionable part survives


def test_the_mail_has_no_message_id(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """The deliberate difference from the reminder path, asserted so it is not "fixed".

    A reminder derives its ``Message-ID`` from its idempotency slot so a re-send after
    a crash is recognisable as a duplicate. An alert is never re-sent, so a derived id
    would make the second finding of the week look like the first to any provider that
    de-duplicates on this header — the one way this message could vanish silently.
    """
    report = _report_for(session_factory, settings, contract)
    alerter, sent = _composer()

    alerter.raised(report)

    assert "Message-ID" not in sent[0]


# --- settings decide whether an alert exists at all -----------------------
def _smtp_settings(**overrides: Any) -> Settings:
    """A complete, valid SMTP configuration the alert can ride on."""
    values: dict[str, Any] = {
        "app_env": "test",
        "log_json": False,
        "notification_transport": "smtp",
        "smtp_host": "relay.example.test",
        "smtp_from": SENDER_ADDRESS,
    }
    values.update(overrides)
    return Settings(**values)


def test_an_unset_address_builds_no_alerter() -> None:
    """Log-only is a configuration, not a misconfiguration — hence ``None``."""
    assert build_attention_alerter(_smtp_settings()) is None


def test_an_address_builds_an_alerter() -> None:
    built = build_attention_alerter(_smtp_settings(attention_alert_to=OPERATOR_ADDRESS))

    assert isinstance(built, SmtpAttentionAlerter)


def test_the_alert_travels_the_shared_smtp_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reuse under test: one relay, one socket path, one scrubbed error text.

    A second copy of the TLS/auth/timeout block would be a second place for the
    verifying context to be forgotten, so the alert is handed to
    ``SmtpSender.deliver`` — and this pins that the builder wires it there rather than
    to a private conversation of its own.
    """
    delivered: list[EmailMessage] = []

    def record(_self: SmtpSender, message: EmailMessage) -> None:
        delivered.append(message)

    monkeypatch.setattr("app.workers.senders.SmtpSender.deliver", record)
    alerter = build_attention_alerter(_smtp_settings(attention_alert_to=OPERATOR_ADDRESS))
    assert alerter is not None

    # A hand-built report is right here: the question is whether the *transport* is
    # reached, and the numbers are incidental.
    report = AttentionReport(
        outbox_parked=1,
        notifications_failed=0,
        notifications_suppressed=0,
        escalations_overdue=0,
        unsealed_field_values=0,
        unsealed_field_ids=(),
        fields_scanned=0,
        field_scan_truncated=False,
        event_transport="kafka",
        bus_disabled=False,
    )
    alerter.raised(report)

    assert len(delivered) == 1
    assert delivered[0]["To"] == OPERATOR_ADDRESS
    assert delivered[0]["Subject"] == SUBJECT_RAISED


def test_settings_refuse_an_alert_address_that_is_not_an_address() -> None:
    with pytest.raises(ValueError, match="ATTENTION_ALERT_TO must be an e-mail address"):
        _smtp_settings(attention_alert_to="the ops channel")


def test_settings_refuse_an_alert_address_with_no_transport() -> None:
    """Reading as "the operator is being told" while the first finding goes nowhere.

    The first finding is precisely when it matters, so the mismatch is refused at boot
    rather than discovered then.
    """
    with pytest.raises(ValueError, match="no transport to send the alert with"):
        Settings(
            app_env="test",
            log_json=False,
            notification_transport="none",
            attention_alert_to=OPERATOR_ADDRESS,
        )
