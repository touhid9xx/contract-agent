"""The attention check that STATUS.md asks for and never had.

Two clauses of the signed GATE 2 record are the same defect: a notification that
parks ``failed`` or is ``suppressed`` raises no alert, and a field value written
before gap 20 stays plaintext with nothing pointing at it. Both are states that
need a person and are invisible until somebody writes a query by hand.

``doctor`` turns them into one number and one exit code. The properties worth
pinning are not "it prints something":

* **Silence is earned, not default.** An empty database must produce a clean
  report, or the command is noise nobody schedules.
* **The pre-gap-20 shape is the one that matters.** A row sealed on ``raw_value``
  and plaintext on ``normalized_value`` is exactly what the old writer left
  behind; it must be reported, and it is the case a naive "is the row encrypted?"
  check misses.
* **No key required.** ``is_sealed`` is shape-based on purpose, so a wrong or
  absent ``ENCRYPTION_KEY`` must still report plaintext as plaintext. A check that
  needed the key would fall silent precisely when the key was wrong.
* **The scan limit is a floor, and says so.** Reporting a truncated scan as clean
  is the one failure mode that would make this command worse than nothing, so the
  truncation flag is asserted directly rather than trusted.
* **Read-only.** An inspection command that seals, retries or resolves anything on
  its own initiative is a repair script wearing the wrong name.
* **A park under a switched-off bus is counted and not failed.** The judgement
  ``D10-39`` adds, and the one worth arguing about: ``EVENT_TRANSPORT=none`` is the
  default, and with it the relay parks *every* event it claims, so failing on a
  park makes this command fail on every run of a default installation until the
  operator learns to ignore it. The count stays visible; the exit code is not
  spent. Both halves are asserted, and so is the narrowness of it: an explained
  park must not mask a real finding sitting next to it.
"""

from __future__ import annotations

from datetime import date

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session, sessionmaker

from app.cli import EXIT_ATTENTION, EXIT_OK, build_parser
from app.cli import main as cli_main
from app.core.attention import attention_report
from app.core.config import Settings
from app.core.crypto import FieldCipher, is_sealed
from app.db.base import utc_now
from app.db.enums import (
    Channel,
    ContractStatus,
    FieldName,
    FieldStatus,
    NotificationStatus,
    OutboxStatus,
)
from app.db.models import Contract, ExtractedField, Notification, OutboxEvent, Tenant
from app.db.session import session_scope
from app.workers.dispatch import notification_idempotency_key

PLAINTEXT_EMAIL = "a.rahman@example.test"
PLAINTEXT_PHONE = "+442079460958"

KEY = Fernet.generate_key().decode("ascii")
CIPHER = FieldCipher(KEY)


@pytest.fixture
def contract(session_factory: sessionmaker[Session], tenant: Tenant) -> Contract:
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


def add_field(
    session_factory: sessionmaker[Session],
    contract: Contract,
    *,
    field_name: FieldName = FieldName.EMAIL,
    raw: str = PLAINTEXT_EMAIL,
    normalized: str = PLAINTEXT_EMAIL,
    seal_raw: bool = False,
    seal_normalized: bool = False,
) -> int:
    """Insert one field row, choosing per column whether it is sealed.

    Per column rather than per row because the interesting legacy shape is a
    *mixed* row: ``raw_value`` sealed while ``normalized_value`` was still written
    in the clear.
    """
    with session_scope(session_factory) as session:
        row = ExtractedField(
            tenant_id=contract.tenant_id,
            contract_id=contract.id,
            field_name=field_name,
            raw_value=CIPHER.encrypt(raw) if seal_raw else raw,
            normalized_value=CIPHER.encrypt(normalized) if seal_normalized else normalized,
            confidence=0.93,
            status=FieldStatus.APPROVED,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def add_outbox_event(
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    *,
    status: OutboxStatus = OutboxStatus.FAILED,
) -> int:
    with session_scope(session_factory) as session:
        row = OutboxEvent(
            correlation_id="corr-doctor",
            tenant_id=tenant.id,
            event_type="contract.ingested",
            topic="contract.events",
            partition_key="1",
            payload={"contract_id": 1},
            status=status,
            # A parked row is a row that spent its budget; a pending one is not.
            attempts=5 if status is OutboxStatus.FAILED else 0,
            next_attempt_at=utc_now(),
            last_error="broker refused" if status is OutboxStatus.FAILED else None,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def add_notification(
    session_factory: sessionmaker[Session],
    contract: Contract,
    *,
    status: NotificationStatus,
    template: str = "T-60",
    slot: date | None = None,
) -> int:
    """Insert one notification row in a chosen state, bypassing the dispatcher.

    Reaching ``failed``/``suppressed`` through the real worker needs a refusing
    transport and a spent retry budget; the state is the subject here, not how it
    was reached.
    """
    scheduled_date = slot if slot is not None else date(2025, 1, 2)
    with session_scope(session_factory) as session:
        row = Notification(
            tenant_id=contract.tenant_id,
            contract_id=contract.id,
            channel=Channel.EMAIL,
            template=template,
            status=status,
            scheduled_date=scheduled_date,
            idempotency_key=notification_idempotency_key(contract.id, template, scheduled_date),
            attempts=5 if status is NotificationStatus.FAILED else 0,
            last_error="relay refused" if status is NotificationStatus.FAILED else None,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def report(
    session_factory: sessionmaker[Session],
    *,
    settings: Settings,
    limit: int = 500,
) -> dict[str, object]:
    """One reading, as the flat shape both callers emit.

    ``settings`` is required rather than defaulted because the reading now depends
    on it: whether a parked event counts as a fault is decided by the transport the
    check was run under. A default would let a test assert the old verdict by
    accident.
    """
    with session_scope(session_factory) as session:
        return attention_report(session, limit=limit, settings=settings).as_dict()


def run(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *argv: str,
) -> int:
    return cli_main(
        ["doctor", *argv],
        settings=settings,
        session_factory=session_factory,
    )


# --- a clean database is silent -------------------------------------------


def test_an_empty_database_needs_no_attention(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    assert run(session_factory, settings) == EXIT_OK


def test_a_clean_report_counts_nothing_at_all(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    """The whole shape, asserted exactly rather than key by key.

    An exact comparison is deliberate: a field added to the report and forgotten by
    a caller is the defect the record type exists to prevent, and this is the one
    place that notices it. ``bus_disabled`` is ``True`` here because ``test``
    settings carry the default ``EVENT_TRANSPORT=none`` — the reading now records
    the transport it was taken under, so the clean case has to say so too.
    """
    assert report(session_factory, settings=settings) == {
        "outbox_parked": 0,
        "outbox_parked_explained": 0,
        "notifications_failed": 0,
        "notifications_suppressed": 0,
        "unsealed_field_values": 0,
        "unsealed_field_ids": [],
        "fields_scanned": 0,
        "field_scan_truncated": False,
        "event_transport": "none",
        "bus_disabled": True,
        "needs_attention": False,
    }


# --- unsealed field values (gap 20's residue) -----------------------------


def test_a_plaintext_field_value_is_reported(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    field_id = add_field(session_factory, contract)

    assert report(session_factory, settings=settings)["unsealed_field_ids"] == [field_id]
    assert run(session_factory, settings) == EXIT_ATTENTION


def test_a_sealed_field_value_is_not_reported(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    add_field(session_factory, contract, seal_raw=True, seal_normalized=True)

    assert report(session_factory, settings=settings)["unsealed_field_values"] == 0
    assert run(session_factory, settings) == EXIT_OK


def test_the_row_the_old_writer_left_behind_is_reported(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """Sealed ``raw_value`` with a plaintext ``normalized_value``.

    This is the exact shape ``reseal-fields`` exists to repair, and the shape a
    check that only asks "is the row encrypted?" answers wrongly.
    """
    field_id = add_field(session_factory, contract, seal_raw=True, seal_normalized=False)

    assert report(session_factory, settings=settings)["unsealed_field_ids"] == [field_id]
    assert run(session_factory, settings) == EXIT_ATTENTION


def test_every_unsealed_row_is_counted_not_just_the_first(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    first = add_field(session_factory, contract, field_name=FieldName.EMAIL)
    second = add_field(
        session_factory,
        contract,
        field_name=FieldName.PHONE,
        raw=PLAINTEXT_PHONE,
        normalized=PLAINTEXT_PHONE,
    )
    # A sealed row between the two plaintext ones proves the count is not just
    # "did the scan stop after the first match".
    add_field(
        session_factory,
        contract,
        field_name=FieldName.ADDRESS,
        seal_raw=True,
        seal_normalized=True,
    )

    assert report(session_factory, settings=settings)["unsealed_field_ids"] == [first, second]


def test_a_row_with_no_values_at_all_is_not_unsealed(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """A NULL column has never held PII, so it needs no sealing."""
    with session_scope(session_factory) as session:
        session.add(
            ExtractedField(
                tenant_id=contract.tenant_id,
                contract_id=contract.id,
                field_name=FieldName.EMAIL,
                raw_value=None,
                normalized_value=None,
                confidence=0.4,
                status=FieldStatus.PENDING_REVIEW,
            )
        )

    assert report(session_factory, settings=settings)["unsealed_field_values"] == 0
    assert run(session_factory, settings) == EXIT_OK


# --- the scan limit is a floor, and admits it -----------------------------


def test_a_full_page_is_reported_as_truncated(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """One row scanned at ``--limit 1`` fills the page.

    Conservative on purpose: the command cannot distinguish "one row exists" from
    "one of many", so it says the count is a floor in both cases.
    """
    add_field(session_factory, contract, seal_raw=True, seal_normalized=True)

    assert report(session_factory, limit=1, settings=settings)["field_scan_truncated"] is True


def test_the_scan_limit_can_hide_a_plaintext_row_beyond_it(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """The acknowledged weakness, asserted rather than glossed.

    The sealed row sorts first, so a one-row page never reaches the plaintext one
    and the report reads clean. This is why the truncation flag exists and why
    ``--limit`` must exceed the field table on a real run.
    """
    add_field(session_factory, contract, seal_raw=True, seal_normalized=True)
    add_field(session_factory, contract, field_name=FieldName.PHONE)

    assert report(session_factory, limit=1, settings=settings)["unsealed_field_values"] == 0
    assert report(session_factory, limit=2, settings=settings)["unsealed_field_values"] == 1


# --- parked outbox rows: a fault, or a configured refusal ----------------
#
# The two halves are the same row and different verdicts, and both have to be
# asserted: with EVENT_TRANSPORT=none (the default, and what `settings` carries)
# the relay parks everything it claims, so a park is the designed shape of a fresh
# installation. With a transport configured, the same park means the relay tried
# and could not deliver, and that is a person's problem.


def test_a_parked_event_under_a_configured_transport_needs_attention(
    session_factory: sessionmaker[Session], settings: Settings, tenant: Tenant
) -> None:
    add_outbox_event(session_factory, tenant)
    add_outbox_event(session_factory, tenant)
    # Built rather than copied from the `settings` fixture: the transport is the
    # only thing under test here, and a real `Settings` re-runs the cross-field
    # validation that makes "kafka with no broker" an error at startup — which is
    # the configuration this test is *not* describing.
    configured = Settings(
        app_env="test",
        log_json=False,
        event_transport="kafka",
        kafka_bootstrap_servers="kafka:9092",
    )

    counts = report(session_factory, settings=configured)

    assert counts["outbox_parked"] == 2
    assert counts["outbox_parked_explained"] == 0
    assert counts["needs_attention"] is True
    assert run(session_factory, configured) == EXIT_ATTENTION


def test_a_park_under_a_switched_off_bus_is_counted_and_not_a_fault(
    session_factory: sessionmaker[Session], settings: Settings, tenant: Tenant
) -> None:
    """The default install parks everything, and must not be told it is broken.

    The count stays — an operator can still see the queue is not draining, and the
    line says why — but no exit code is spent on it. A command that failed on every
    default installation is one nobody schedules, which is how the one failure it
    *can* report stops being read (``D10-39``).
    """
    add_outbox_event(session_factory, tenant)
    add_outbox_event(session_factory, tenant)

    counts = report(session_factory, settings=settings)

    assert counts["outbox_parked"] == 2
    assert counts["outbox_parked_explained"] == 2
    assert counts["bus_disabled"] is True
    assert counts["needs_attention"] is False
    assert run(session_factory, settings) == EXIT_OK


def test_an_explained_park_does_not_mask_a_real_finding_beside_it(
    session_factory: sessionmaker[Session],
    settings: Settings,
    tenant: Tenant,
    contract: Contract,
) -> None:
    """The narrowness of the suppression, asserted.

    Only the park is explained; everything else in the same reading still decides
    the verdict. Without this, "explained" could grow into "this run says nothing".
    """
    add_outbox_event(session_factory, tenant)
    add_field(session_factory, contract)

    counts = report(session_factory, settings=settings)

    assert counts["outbox_parked_explained"] == 1
    assert counts["unsealed_field_values"] == 1
    assert counts["needs_attention"] is True
    assert run(session_factory, settings) == EXIT_ATTENTION


def test_a_pending_event_is_not_counted(
    session_factory: sessionmaker[Session], settings: Settings, tenant: Tenant
) -> None:
    """Pending is the normal state of every event between commit and relay."""
    add_outbox_event(session_factory, tenant, status=OutboxStatus.PENDING)

    assert report(session_factory, settings=settings)["outbox_parked"] == 0
    assert run(session_factory, settings) == EXIT_OK


# --- undelivered notifications --------------------------------------------


def test_failed_and_suppressed_notifications_are_counted_separately(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """Two different operator responses: ``failed`` is retryable, ``suppressed``
    was a policy refusal and needs a human to change consent or the channel."""
    add_notification(session_factory, contract, status=NotificationStatus.FAILED)
    add_notification(
        session_factory,
        contract,
        status=NotificationStatus.SUPPRESSED,
        template="T-30",
        slot=date(2025, 1, 3),
    )

    counts = report(session_factory, settings=settings)

    assert counts["notifications_failed"] == 1
    assert counts["notifications_suppressed"] == 1


def test_a_sent_notification_is_not_counted(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    add_notification(session_factory, contract, status=NotificationStatus.SENT)
    add_notification(
        session_factory,
        contract,
        status=NotificationStatus.PENDING,
        template="T-30",
        slot=date(2025, 1, 3),
    )

    counts = report(session_factory, settings=settings)

    assert counts["notifications_failed"] == 0
    assert counts["notifications_suppressed"] == 0
    assert run(session_factory, settings) == EXIT_OK


# --- the properties that make it safe to schedule -------------------------


def test_it_runs_without_an_encryption_key_and_still_sees_plaintext(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """``is_sealed`` is shape-based, so the check does not need the key.

    A key-dependent check would go silent whenever the key was missing or wrong —
    reporting a plaintext database as clean at the one moment it mattered most.
    """
    add_field(session_factory, contract)
    keyless = settings.model_copy(update={"encryption_key": None})

    assert run(session_factory, keyless) == EXIT_ATTENTION


def test_a_sealed_value_under_a_rotated_key_is_not_reported_as_plaintext(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """A token this key cannot read is still a token, and must not be flagged.

    Flagging it would send the operator to ``reseal-fields``, which would then
    refuse with ``EXIT_UNREADABLE`` — a false positive that costs a run and teaches
    the operator to ignore the command.
    """
    other = FieldCipher(Fernet.generate_key().decode("ascii"))
    with session_scope(session_factory) as session:
        session.add(
            ExtractedField(
                tenant_id=contract.tenant_id,
                contract_id=contract.id,
                field_name=FieldName.EMAIL,
                raw_value=other.encrypt(PLAINTEXT_EMAIL),
                normalized_value=other.encrypt(PLAINTEXT_EMAIL),
                confidence=0.9,
                status=FieldStatus.APPROVED,
            )
        )

    assert report(session_factory, settings=settings)["unsealed_field_values"] == 0
    assert run(session_factory, settings) == EXIT_OK


def test_running_it_changes_nothing(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """Read-only: the plaintext row is still plaintext afterwards.

    Sealing is ``reseal-fields``' job. If this command repaired what it found, an
    inspection run on a shared database would quietly rewrap values under whatever
    key the operator happened to have exported.
    """
    field_id = add_field(session_factory, contract)

    run(session_factory, settings)

    with session_scope(session_factory) as session:
        stored = session.get(ExtractedField, field_id)
        assert stored is not None
        assert stored.normalized_value == PLAINTEXT_EMAIL
        assert not is_sealed(str(stored.normalized_value))


def test_the_report_carries_ids_and_never_values(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    field_id = add_field(session_factory, contract)

    rendered = repr(report(session_factory, settings=settings))

    assert PLAINTEXT_EMAIL not in rendered
    assert str(field_id) in rendered


# --- the command is wired into the parser ---------------------------------


def test_the_command_is_reachable_from_the_parser() -> None:
    args = vars(build_parser().parse_args(["doctor"])).get("handler")
    assert callable(args)


def test_the_limit_flag_parses_and_defaults(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    assert run(session_factory, settings, "--limit", "7") == EXIT_OK
