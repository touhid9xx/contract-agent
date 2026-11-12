"""The escalation alert: the part that tells a person an escalation happened.

``EscalationScanner`` opens escalations. Until this slice, nothing said so out
loud: the row landed in a queue that a human had to already have open, and the
ledger recorded the consequence plainly — "no alert fires when a queue item goes
late". A deadline that nobody is watching is not a deadline.

Five claims are pinned here:

* **Raising is announced once.** The alert rides the raising transition, so a
  second pass over the same contract announces nothing. There is no "already
  told" memory to get wrong, and none is added.
* **One message per scan.** A pass that opens three escalations sends one message
  listing three, not three that bury each other.
* **A refusing relay cannot undo the escalation.** The mail is sent after the
  transaction commits, so the escalation exists regardless. The failure costs a
  counter and a log line, never a rolled-back escalation or an exception out of
  ``run_once``.
* **The alert carries no customer data.** It is an e-mail: it lands on a relay and
  possibly an archive. It names ids and a deadline, which is all an operator needs
  to open the timeline — and all that may leave the application.
* **No address means no alerter, visibly.** ``None`` is a value the scanner logs,
  not a silent no-op that reads as wired up.

The settings validator is pinned here too, in the same spirit as
``tests/test_config.py``: an address with no relay behind it is refused at
start-up rather than discovered at the first escalation.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

import pytest
import structlog
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from app.core import metrics
from app.core.config import Settings
from app.db.enums import ContractStatus
from app.db.models import Tenant
from app.workers.dispatch import SenderError
from app.workers.escalation_alerts import (
    SUBJECT,
    RaisedEscalation,
    SmtpEscalationAlerter,
    _compose,
    build_escalation_alerter,
)
from app.workers.escalation_scanner import EscalationScanner
from tests.test_escalation_scanner import (
    CONTACT_EMAIL,
    CUSTOMER_NAME,
    NO_RESPONSE_DAYS,
    SLA_HOURS,
    add_contact,
    escalations_for,
    load_contract,
    make_contract,
)

OPERATOR_ADDRESS = "ops@example.test"
SENDER_ADDRESS = "agent@example.test"

#: The event the scanner logs when a relay refuses, spelled out rather than
#: imported so a rename fails a test instead of silently agreeing with the code.
EVENT_ALERT_FAILED = "escalation_alert_failed"


# --- doubles --------------------------------------------------------------
class RecordingAlerter:
    """Keeps every batch it was handed, so "how often" is observable."""

    def __init__(self) -> None:
        self.batches: list[tuple[RaisedEscalation, ...]] = []

    def raised(self, escalations: Sequence[RaisedEscalation]) -> None:
        self.batches.append(tuple(escalations))

    @property
    def flat(self) -> list[RaisedEscalation]:
        return [item for batch in self.batches for item in batch]


class RefusingAlerter:
    """A relay that says no. Counts its calls so "tried once" is observable."""

    def __init__(self) -> None:
        self.calls = 0

    def raised(self, escalations: Sequence[RaisedEscalation]) -> None:
        self.calls += 1
        raise SenderError("the relay refused the message")


# --- helpers --------------------------------------------------------------
def settings_for(**overrides: Any) -> Settings:
    """Test settings, built rather than mutated so pydantic validates them."""
    values: dict[str, Any] = {
        "app_env": "test",
        "log_json": False,
        "escalation_no_response_days": NO_RESPONSE_DAYS,
        "escalation_sla_hours": SLA_HOURS,
        "escalation_scan_batch_size": 50,
        "escalation_scan_poll_interval_seconds": 0.01,
    }
    values.update(overrides)
    return Settings(**values)


def scanner_with(
    session_factory: sessionmaker[Session],
    alerter: Any = None,
) -> EscalationScanner:
    return EscalationScanner(session_factory, settings=settings_for(), alerter=alerter)


def a_silent_contract(session_factory: sessionmaker[Session], tenant: Tenant) -> Any:
    """One contract that has gone quiet for longer than the response window."""
    contract = make_contract(session_factory, tenant)
    add_contact(session_factory, contract, days_ago=NO_RESPONSE_DAYS + 1)
    return contract


def failed_alert_count() -> float:
    """Read the counter the spec's alert rule queries, from the scrape text.

    Parsed rather than reached into, because the rendered exposition is the
    contract Prometheus actually consumes -- and the family name has to survive
    verbatim for ``increase(escalation_email_failed_total[1h]) > 0`` to work.
    """
    for line in metrics.render().splitlines():
        if line.startswith("escalation_email_failed_total"):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def a_raised_escalation(
    *,
    contract_id: int = 1042,
    escalation_id: int = 7,
    sla_deadline: datetime | None = None,
) -> RaisedEscalation:
    return RaisedEscalation(
        tenant_id=UUID("00000000-0000-0000-0000-0000000000aa"),
        contract_id=contract_id,
        escalation_id=escalation_id,
        sla_deadline=sla_deadline or datetime(2026, 7, 1, 9, 0, 0),
    )


# --- the announcement -----------------------------------------------------
def test_a_raised_escalation_reaches_the_operator(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """The whole feature: silence long enough, so somebody is told."""
    contract = a_silent_contract(session_factory, tenant)
    alerter = RecordingAlerter()

    result = scanner_with(session_factory, alerter).run_once()

    assert result.opened == 1
    assert len(alerter.batches) == 1
    (item,) = alerter.flat
    row = escalations_for(session_factory, contract.id)[0]
    assert item.escalation_id == row.id
    assert item.contract_id == contract.id
    assert item.tenant_id == tenant.id
    # The deadline in the message is the one the queue will hold somebody to.
    assert item.sla_deadline == row.sla_deadline


def test_a_quiet_pass_tells_nobody(session_factory: sessionmaker[Session], tenant: Tenant) -> None:
    """Recent contact is not an escalation, so there is nothing to announce."""
    contract = make_contract(session_factory, tenant)
    add_contact(session_factory, contract, days_ago=NO_RESPONSE_DAYS - 1)
    alerter = RecordingAlerter()

    assert scanner_with(session_factory, alerter).run_once().opened == 0
    assert alerter.batches == []


def test_one_message_per_pass_not_one_per_escalation(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """Three escalations in one tick is one event, not three e-mails."""
    contracts = [a_silent_contract(session_factory, tenant) for _ in range(3)]
    alerter = RecordingAlerter()

    result = scanner_with(session_factory, alerter).run_once()

    assert result.opened == 3
    assert len(alerter.batches) == 1
    assert {item.contract_id for item in alerter.batches[0]} == {row.id for row in contracts}


def test_a_second_pass_does_not_announce_the_same_escalation_again(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """Raising is a transition, so it happens once and is announced once.

    This is why no "already told" table is needed: the claim query refuses to open
    a second escalation for a contract, so there is no second announcement to
    suppress.
    """
    a_silent_contract(session_factory, tenant)
    alerter = RecordingAlerter()
    worker = scanner_with(session_factory, alerter)

    assert worker.run_once().opened == 1
    assert worker.run_once().opened == 0

    assert len(alerter.batches) == 1
    assert len(alerter.flat) == 1


# --- the failure mode -----------------------------------------------------
def test_a_refusing_relay_does_not_undo_the_escalation(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """The mail is sent after the commit, so a dead relay cannot cost a row.

    This is the ordering that matters: were the alert inside the transaction, a
    refused message would roll back the escalation and the customer would silently
    drop out of the queue entirely -- worse than not being told.
    """
    contract = a_silent_contract(session_factory, tenant)
    refusing = RefusingAlerter()

    result = scanner_with(session_factory, refusing).run_once()

    assert refusing.calls == 1
    assert result.opened == 1
    assert len(escalations_for(session_factory, contract.id)) == 1
    assert load_contract(session_factory, contract.id).status is ContractStatus.ESCALATED


def test_a_refusing_relay_is_counted_for_the_alert_rule(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """``increase(escalation_email_failed_total[1h]) > 0`` needs the increment."""
    a_silent_contract(session_factory, tenant)
    before = failed_alert_count()

    scanner_with(session_factory, RefusingAlerter()).run_once()

    assert failed_alert_count() == before + 1


def test_a_refusing_relay_is_logged_and_the_pass_still_reports_its_work(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """A lost alert is reported, never re-raised out of ``run_once``.

    An exception here would make ``run_forever`` log a failed tick and imply the
    pass wrote nothing, when in fact it wrote everything.
    """
    a_silent_contract(session_factory, tenant)

    with structlog.testing.capture_logs() as captured:
        result = scanner_with(session_factory, RefusingAlerter()).run_once()

    assert result.opened == 1
    failures = [event for event in captured if event.get("event") == EVENT_ALERT_FAILED]
    assert len(failures) == 1
    assert failures[0]["opened"] == 1


# --- what the message may contain ----------------------------------------
def test_the_alert_names_no_customer_data() -> None:
    """It is an e-mail: ids and a deadline, never a name or a contact value."""
    composed = _compose(
        [a_raised_escalation()],
        sender=SENDER_ADDRESS,
        recipient=OPERATOR_ADDRESS,
    )
    body = composed.get_content()

    assert CONTACT_EMAIL not in body
    assert CUSTOMER_NAME not in body
    assert composed["Subject"] == SUBJECT
    assert composed["To"] == OPERATOR_ADDRESS


def test_the_alert_carries_what_an_operator_needs_to_act() -> None:
    """Ids and a deadline: enough to open the timeline, and nothing more."""
    body = _compose(
        [a_raised_escalation(contract_id=1042, escalation_id=7)],
        sender=SENDER_ADDRESS,
        recipient=OPERATOR_ADDRESS,
    ).get_content()

    assert "contract 1042" in body
    assert "escalation 7" in body
    assert "1 raised" in body
    # UTC is stated, so a deadline cannot be read as local time.
    assert "2026-07-01T09:00:00 (UTC)" in body


def test_the_count_in_the_body_follows_the_batch() -> None:
    """One message listing three says three."""
    body = _compose(
        [a_raised_escalation(contract_id=row) for row in (1, 2, 3)],
        sender=SENDER_ADDRESS,
        recipient=OPERATOR_ADDRESS,
    ).get_content()

    assert "3 raised" in body


# --- construction and configuration --------------------------------------
def test_no_address_means_no_alerter() -> None:
    """``None`` is how the scanner can log that nobody is being told."""
    assert build_escalation_alerter(settings_for()) is None


def test_a_configured_address_builds_an_smtp_alerter() -> None:
    alerter = build_escalation_alerter(
        settings_for(
            notification_transport="smtp",
            escalation_alert_to=OPERATOR_ADDRESS,
            smtp_host="127.0.0.1",
            smtp_from=SENDER_ADDRESS,
        )
    )

    assert isinstance(alerter, SmtpEscalationAlerter)


def test_an_address_without_a_transport_is_refused_at_start_up() -> None:
    """An address with no relay behind it reads as "a person is being told"."""
    with pytest.raises(ValidationError) as raised:
        settings_for(notification_transport="none", escalation_alert_to=OPERATOR_ADDRESS)

    assert "ESCALATION_ALERT_TO" in str(raised.value)


def test_a_malformed_address_is_refused_at_start_up() -> None:
    with pytest.raises(ValidationError) as raised:
        settings_for(
            notification_transport="smtp",
            escalation_alert_to="not-an-address",
            smtp_host="127.0.0.1",
            smtp_from=SENDER_ADDRESS,
        )

    assert "ESCALATION_ALERT_TO" in str(raised.value)
