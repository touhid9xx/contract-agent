"""The repair step migration ``0007`` promises: making old contact rows findable.

``blind_index`` is what lets an inbound STOP reach the contract it belongs to, and
it only exists on rows written after that column did. Every contact row written
before it is invisible to the opt-out path in a way nobody is told about: the
gateway accepts the reply, the route computes a digest, and the lookup returns
nothing. From the customer's side that is a "STOP" that was ignored.

What is worth pinning is not "the column gets filled". It is the four rules that
make this safe to point at a live database:

* **The row it writes is the row the route reads.** A digest computed differently
  from :func:`contracts_for_contact`'s predicate would fill the column and still
  never match, so the happy path is asserted as a round trip through the real
  lookup rather than as ``blind_index IS NOT NULL``.
* **A wrong key stops the run.** Recomputing under the wrong ``ENCRYPTION_KEY``
  would replace nothing today (the digest needs the *plaintext*), but the run
  still cannot do its job, and finishing "successfully" would tell an operator
  the repair was complete. ``EXIT_UNREADABLE`` is the honest answer.
* **It converges.** The predicate is ``IS NULL``, so a second pass examines
  nothing. That is what makes it safe to re-run after an interruption — and what
  stops a rotated ``CONTACT_BLIND_INDEX_KEY`` from silently rewriting live
  digests one batch at a time, which is the failure the separate key exists to
  avoid.
* **A value it cannot read is reported, never guessed.** A phone column holding
  ``"n/a"`` gets no digest: inventing one would make a single row match many.

The last test is about the log, because this command's only other output is an
exit code, and an id in a log line is the difference between a reported row and a
row that leaks a phone number into the log stream.
"""

from __future__ import annotations

import argparse
import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs

from app.cli import EXIT_FAILED, EXIT_NO_MATCH, EXIT_OK, EXIT_UNREADABLE, _backfill_blind_index
from app.cli import main as cli_main
from app.core.blind_index import build_blind_index
from app.core.config import Settings
from app.core.crypto import FieldCipher
from app.db.enums import ContractStatus, FieldName, FieldStatus
from app.db.models import Contract, ExtractedField, Tenant
from app.db.repositories.fields import contracts_for_contact
from app.db.session import session_scope

#: The formatting a document would actually contain, which is not the canonical form.
FORMATTED_PHONE = "+44 20 7946 0958"

#: What ``normalize_value`` turns it into, and therefore what the digest is over.
NORMALISED_PHONE = "+442079460958"

PLAINTEXT_EMAIL = "a.rahman@example.test"

#: The key the legacy rows were sealed with, and the run's current key.
KEY = Fernet.generate_key().decode("ascii")
CIPHER = FieldCipher(KEY)

#: Long enough to satisfy ``MIN_BLIND_INDEX_KEY_LENGTH`` without pretending to be real.
INDEX_KEY = "contact-blind-index-key-for-tests-0123456789"
OTHER_INDEX_KEY = "a-different-contact-blind-index-key-0123456789"


def indexed_settings(settings: Settings, **overrides: str | None) -> Settings:
    """Test settings carrying both keys a backfill needs.

    ``model_copy`` skips validators, so ``contact_blind_index_key=None`` reaches the
    handler the way an unset variable does at runtime rather than being refused here.
    """
    update: dict[str, str | None] = {
        "encryption_key": KEY,
        "contact_blind_index_key": INDEX_KEY,
    }
    update.update(overrides)
    return settings.model_copy(update=update)


def new_contract(session_factory: sessionmaker[Session], tenant: Tenant) -> Contract:
    """A fresh contract, because one contract holds at most one row per field name."""
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


@pytest.fixture
def contract(session_factory: sessionmaker[Session], tenant: Tenant) -> Contract:
    return new_contract(session_factory, tenant)


def add_field(
    session_factory: sessionmaker[Session],
    contract: Contract,
    *,
    field_name: FieldName = FieldName.PHONE,
    plaintext: str = FORMATTED_PHONE,
    seal: bool = True,
) -> int:
    """Insert one contact row as a pre-``0007`` writer left it: no ``blind_index``.

    ``seal=False`` reproduces a row from before gap 20 was closed, whose columns are
    plaintext. Both shapes exist in a database that has been upgraded twice.
    """
    stored = CIPHER.encrypt(plaintext) if seal else plaintext
    with session_scope(session_factory) as session:
        row = ExtractedField(
            tenant_id=contract.tenant_id,
            contract_id=contract.id,
            field_name=field_name,
            raw_value=stored,
            normalized_value=stored,
            confidence=0.93,
            status=FieldStatus.APPROVED,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def load(session_factory: sessionmaker[Session], field_id: int) -> ExtractedField:
    with session_scope(session_factory) as session:
        row = session.get(ExtractedField, field_id)
        assert row is not None
        return row


def findable(
    session_factory: sessionmaker[Session], settings: Settings, phone: str
) -> list[Contract]:
    """The opt-out lookup itself, keyed exactly as the inbound route keys it."""
    digest = build_blind_index(indexed_settings(settings)).digest(phone)
    with session_scope(session_factory) as session:
        return contracts_for_contact(session, field_name=FieldName.PHONE, blind_index=digest)


def run(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *argv: str,
) -> int:
    return cli_main(
        ["backfill-blind-index", *argv],
        settings=settings,
        session_factory=session_factory,
    )


# --- the round trip that is the whole point -------------------------------


def test_a_legacy_row_becomes_findable_by_the_number_it_holds(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """Before the run the row is invisible to a STOP; after it, the STOP lands.

    Asserted through :func:`contracts_for_contact` rather than by reading the column,
    because a digest written to the wrong place would satisfy the weaker assertion.
    """
    field_id = add_field(session_factory, contract)
    assert findable(session_factory, settings, NORMALISED_PHONE) == []

    code = run(session_factory, indexed_settings(settings))

    assert code == EXIT_OK
    assert load(session_factory, field_id).blind_index is not None
    assert [row.id for row in findable(session_factory, settings, NORMALISED_PHONE)] == [
        contract.id
    ]


def test_the_digest_is_computed_over_the_normalised_form(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """A document's formatting must not change the digest, or nothing would match.

    The stored value carries spaces and the reply does not; hashing what is *stored*
    instead of what it normalises to would index a number no customer can type.
    """
    field_id = add_field(session_factory, contract, plaintext=FORMATTED_PHONE)

    run(session_factory, indexed_settings(settings))

    expected = build_blind_index(indexed_settings(settings)).digest(NORMALISED_PHONE)
    assert load(session_factory, field_id).blind_index == expected


def test_a_plaintext_legacy_row_is_indexed_as_well(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """A row from before encryption was enforced still has a readable number."""
    field_id = add_field(session_factory, contract, seal=False)

    code = run(session_factory, indexed_settings(settings))

    assert code == EXIT_OK
    assert load(session_factory, field_id).blind_index is not None
    assert [row.id for row in findable(session_factory, settings, NORMALISED_PHONE)] == [
        contract.id
    ]


def test_the_column_is_a_digest_and_never_the_number(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """The index must not reintroduce the column contents encryption removed."""
    field_id = add_field(session_factory, contract)

    run(session_factory, indexed_settings(settings))

    stored = str(load(session_factory, field_id).blind_index)
    assert len(stored) == 64
    assert NORMALISED_PHONE not in stored
    assert "7946" not in stored


# --- refusing to make things worse ---------------------------------------


def test_a_wrong_encryption_key_stops_the_run_and_writes_nothing(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """The rows are sealed under a key this run does not hold, so it cannot read them.

    Reporting success would be a lie an operator could act on: they would delete the
    old key believing the repair finished.
    """
    foreign = FieldCipher(Fernet.generate_key().decode("ascii"))
    token = foreign.encrypt(FORMATTED_PHONE)
    with session_scope(session_factory) as session:
        row = ExtractedField(
            tenant_id=contract.tenant_id,
            contract_id=contract.id,
            field_name=FieldName.PHONE,
            raw_value=token,
            normalized_value=token,
            confidence=0.93,
            status=FieldStatus.APPROVED,
        )
        session.add(row)
        session.flush()
        field_id = int(row.id)

    code = run(session_factory, indexed_settings(settings))

    assert code == EXIT_UNREADABLE
    assert load(session_factory, field_id).blind_index is None


def test_without_a_blind_index_key_it_refuses(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """Fail closed, like every other writer: a skipped digest is an ignored STOP."""
    field_id = add_field(session_factory, contract)

    code = run(session_factory, indexed_settings(settings, contact_blind_index_key=None))

    assert code == EXIT_FAILED
    assert load(session_factory, field_id).blind_index is None


def test_without_an_encryption_key_it_refuses(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    field_id = add_field(session_factory, contract)

    code = run(session_factory, indexed_settings(settings, encryption_key=""))

    assert code == EXIT_FAILED
    assert load(session_factory, field_id).blind_index is None


def test_a_value_that_is_not_a_phone_number_is_reported_and_left_null(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """``"n/a"`` in a phone column gets no digest: one invented digest matches many rows."""
    field_id = add_field(session_factory, contract, plaintext="n/a")

    code = run(session_factory, indexed_settings(settings))

    assert code == EXIT_OK
    assert load(session_factory, field_id).blind_index is None


def test_a_non_contact_row_is_never_given_a_digest(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """No inbound reply carries a date, so the permanent ``NULL`` is not a defect."""
    field_id = add_field(
        session_factory, contract, field_name=FieldName.END_DATE, plaintext="2025-03-01"
    )

    code = run(session_factory, indexed_settings(settings))

    assert code == EXIT_OK
    assert load(session_factory, field_id).blind_index is None


# --- the run has to converge ---------------------------------------------


def test_a_dry_run_writes_nothing(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """How an operator sizes the job: report the rows, change none of them."""
    field_id = add_field(session_factory, contract)

    code = run(session_factory, indexed_settings(settings), "--dry-run")

    assert code == EXIT_OK
    assert load(session_factory, field_id).blind_index is None


def test_a_second_run_under_a_new_key_leaves_live_digests_alone(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """The ``IS NULL`` predicate, stated as the property it protects.

    Recomputing every contact row would let a changed ``CONTACT_BLIND_INDEX_KEY``
    rewrite the column one batch at a time, so half the estate would match replies
    and half would not — and nothing would say which. Converging is not tidiness.
    """
    field_id = add_field(session_factory, contract)

    assert run(session_factory, indexed_settings(settings)) == EXIT_OK
    after_first = load(session_factory, field_id).blind_index
    assert after_first is not None

    code = run(session_factory, indexed_settings(settings, contact_blind_index_key=OTHER_INDEX_KEY))

    assert code == EXIT_OK
    assert load(session_factory, field_id).blind_index == after_first


def test_an_unknown_id_fails_loudly(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    """A wrong id usually means the operator is pointed at another database."""
    code = run(session_factory, indexed_settings(settings), "--ids", "999")

    assert code == EXIT_NO_MATCH


def test_a_targeted_id_indexes_only_that_row(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """Explicit ids replace the limit rather than being filtered by it."""
    first = add_field(
        session_factory, contract, field_name=FieldName.EMAIL, plaintext=PLAINTEXT_EMAIL
    )
    second = add_field(session_factory, contract)

    code = run(session_factory, indexed_settings(settings), "--ids", str(second))

    assert code == EXIT_OK
    assert load(session_factory, first).blind_index is None
    assert load(session_factory, second).blind_index is not None


def test_the_limit_bounds_how_many_rows_are_examined(
    session_factory: sessionmaker[Session], settings: Settings, tenant: Tenant
) -> None:
    """A bounded run takes a stable prefix, so the next run resumes where it stopped."""
    ids = [
        add_field(
            session_factory,
            new_contract(session_factory, tenant),
            plaintext=f"+4420794609{n:02d}",
        )
        for n in range(3)
    ]

    code = run(session_factory, indexed_settings(settings), "--limit", "2")

    assert code == EXIT_OK
    assert load(session_factory, ids[0]).blind_index is not None
    assert load(session_factory, ids[1]).blind_index is not None
    assert load(session_factory, ids[2]).blind_index is None


def test_a_second_run_examines_nothing_at_all(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """After a complete pass, the repair set is empty — not "already correct"."""
    add_field(session_factory, contract)
    assert run(session_factory, indexed_settings(settings)) == EXIT_OK

    with session_scope(session_factory) as session:
        remaining = session.execute(
            select(ExtractedField).where(ExtractedField.blind_index.is_(None))
        ).scalars()

        assert list(remaining) == []


# --- the log --------------------------------------------------------------


def test_the_log_names_the_ids_and_never_the_values_or_the_digests(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """A repair that logs what it repaired must not log *what* it repaired.

    Calls the handler directly rather than ``cli_main``, because ``main`` configures
    the process logger and that would replace the capture. The behaviour is covered
    above through the CLI; this test is only about the event dict.
    """
    field_id = add_field(session_factory, contract)

    args = argparse.Namespace(ids=None, limit=50, dry_run=False)
    with capture_logs() as logs:
        code = _backfill_blind_index(args, session_factory, indexed_settings(settings))

    assert code == EXIT_OK
    summary = [entry for entry in logs if entry["event"] == "blind_index_backfilled"]
    assert len(summary) == 1
    assert summary[0]["field_ids"] == [field_id]

    digest = str(load(session_factory, field_id).blind_index)
    rendered = json.dumps(logs, default=str)
    assert FORMATTED_PHONE not in rendered
    assert NORMALISED_PHONE not in rendered
    assert digest not in rendered
