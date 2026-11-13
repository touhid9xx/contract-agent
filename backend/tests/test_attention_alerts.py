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

One thing about the text is asserted **structurally** rather than by quoting it: the
raised headline must name every state that can raise the verdict, and the states are
taken from ``needs_attention`` itself rather than from a remembered list. ``D10-99``
added a fourth finding to the report and left the prose naming three; nothing failed,
because the counts were right, the log line was right, and only the sentence above
them described a smaller board than the operator was looking at.
"""

from __future__ import annotations

import dataclasses
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
    _EXPLAINED_PARKS,
    _RAISED_HEADLINE,
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


# --- the headline has to name what the body counts ------------------------
#: One identifying phrase per state that can raise the verdict.
#:
#: ``_RAISED_HEADLINE`` is fixed prose rather than something generated from the
#: report, so it has to name *every* category the body may then count. ``D10-99``
#: added ``escalations_overdue`` to the report and left this prose naming three, so
#: the operator's mail described a board that could raise a fourth finding it never
#: mentioned. Nothing failed, because nothing compared the two. This table does.
_HEADLINE_PHRASES: dict[str, str] = {
    "outbox_parked": "dead-letter",
    "notifications_failed": "failed",
    "notifications_suppressed": "suppressed",
    "escalations_overdue": "escalation",
    "unsealed_field_values": "in the clear",
}


def _calm() -> AttentionReport:
    """A reading with nothing on it, taken under a bus that is switched **on**.

    ``bus_disabled=False`` is load-bearing: with the transport off a parked event is
    ``parks_explained`` and deliberately does not raise the verdict (``D10-39``), so
    every assertion below about ``outbox_parked`` would pass for the wrong reason.
    """
    return AttentionReport(
        outbox_parked=0,
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


def _raised_headline() -> str:
    """The raised prose alone, flattened onto one line.

    Read from the constant rather than from a composed message, and that distinction
    is the whole test. ``_body`` appends ``report.as_dict()``, whose *key names* are
    the same words a phrase search looks for: searching the whole body for
    "escalation" matches ``escalations_overdue: 0`` whether or not the sentence above
    it mentions the finding at all. An earlier version of this guard searched the
    body and passed against a headline that named three findings instead of four —
    a test that could not fail for the reason it was written.

    Flattened because the prose is hard-wrapped at source width: searching it
    verbatim would start failing the day a re-wrap moved a phrase across a line
    break, which is a false failure about formatting rather than about what the
    mail says.
    """
    return " ".join(_RAISED_HEADLINE.split())


def test_the_mail_carries_the_headline_these_tests_read() -> None:
    """So that the prose assertions below are about the message that is really sent.

    Without this the guard would be about a constant nobody has to use: the alerter
    could compose a different headline and every phrase test would still pass.
    """
    alerter, sent = _composer()
    alerter.raised(_calm())
    assert _raised_headline() in " ".join(sent[0].get_content().split())


def test_the_headline_names_each_reason_the_body_may_then_count() -> None:
    """Every tabulated entry is both a real reason and a sentence in the prose.

    Both halves matter. A phrase with no matching reason describes a state that
    cannot happen, and a reason with no phrase is ``D10-99``'s bug: the operator
    reads a message that never mentions what the counts beneath it are counting.
    """
    headline = _raised_headline()

    for name, phrase in _HEADLINE_PHRASES.items():
        report = dataclasses.replace(_calm(), **{name: 1})
        assert report.needs_attention, f"{name} is tabulated as a reason but raises nothing"
        assert phrase in headline, f"the raised headline never mentions {name}"


def test_no_state_raises_the_verdict_without_a_sentence_in_the_headline() -> None:
    """What makes the table exhaustive instead of a snapshot of today.

    Each integer count is raised on its own against a calm baseline. Any that flips
    the verdict must be tabulated — and the previous test then requires its phrase to
    be in the prose. So adding a fifth finding to ``needs_attention`` fails here
    until somebody writes a sentence for it, which is exactly the step ``D10-99``
    skipped by hand and the reason a golden string would not have been enough.

    The limit, stated rather than hidden: only integer counts are probed.
    ``unsealed_field_ids`` is the one non-integer count today and is derived from
    ``unsealed_field_values`` rather than a reason of its own, so it cannot raise the
    verdict alone — a future tuple count that *could* would escape this probe.
    """
    calm = _calm()

    for field in dataclasses.fields(AttentionReport):
        value = getattr(calm, field.name)
        # ``bool`` is an ``int``: a flag turned on is not a count being raised.
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if not dataclasses.replace(calm, **{field.name: value + 1}).needs_attention:
            continue
        assert field.name in _HEADLINE_PHRASES, (
            f"{field.name} raises the verdict, so the raised headline must name it"
        )


def test_the_escalation_clause_says_it_is_not_a_delivery_failure() -> None:
    """It sits beside three delivery failures and reads like a fourth.

    Retrying the send is the one repair that cannot work for it — the notice reached
    the customer, who then went quiet — so the mail has to say so, or the operator's
    first move is the wrong one (``D10-99``).
    """
    assert "not a delivery failure" in _raised_headline()


# --- the park count means something else with the bus switched off --------
def _expected_parks_note() -> str:
    """The paragraph composed for a switched-off event transport, flattened.

    Flattened for the same reason the headline is: the prose is hard-wrapped at source
    width, so searching it verbatim would start failing the day a re-wrap moved a
    phrase across a line break — a false failure about formatting.
    """
    return " ".join(_EXPLAINED_PARKS.split())


def test_the_headline_qualifies_the_park_condition() -> None:
    """The one reading where "a parked event needs a person" is false.

    With no event transport configured a park is what the configuration produces, and
    the check deliberately does not raise on it (``D10-39``). So the headline cannot
    name a park unqualified: the mail would describe a board this reading is not.
    """
    assert "a transport to deliver it" in _raised_headline()

    explained = dataclasses.replace(
        _calm(), outbox_parked=3, bus_disabled=True, event_transport="none"
    )
    assert explained.parks_explained == 3
    assert explained.needs_attention is False


def test_a_mail_from_a_bus_off_deployment_says_the_parks_are_expected() -> None:
    """The count is still printed, so the mail has to say it is not a reason.

    Both directions in one case, because the paragraph is a function of the report
    rather than of the direction: a bus that is *on* must never tell an operator to
    ignore the one count that really is a fault.
    """
    alerter, sent = _composer()
    reason = dataclasses.replace(_calm(), notifications_failed=1)
    switched_off = dataclasses.replace(
        reason, outbox_parked=3, bus_disabled=True, event_transport="none"
    )

    alerter.raised(switched_off)
    alerter.raised(dataclasses.replace(reason, outbox_parked=3))

    bus_off = " ".join(sent[0].get_content().split())
    bus_on = " ".join(sent[1].get_content().split())
    assert _expected_parks_note() in bus_off
    assert _expected_parks_note() not in bus_on


def test_a_bus_off_deployment_with_nothing_parked_gets_the_plain_mail() -> None:
    """A paragraph about zero parks is noise, and this is the common reading.

    A deployment with no transport and a clean board is what every quiet install
    looks like, so it is the one case where the note must stay out of the way.
    """
    alerter, sent = _composer()

    alerter.cleared(dataclasses.replace(_calm(), bus_disabled=True, event_transport="none"))

    assert _expected_parks_note() not in " ".join(sent[0].get_content().split())


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
