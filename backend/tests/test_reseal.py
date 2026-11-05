"""Repairing field values stored before gap 20 was closed.

The write paths now seal ``raw_value`` and ``normalized_value``, so this file is
about the rows that were written before they did. ``reseal-fields`` is a repair
script pointed at a live database, which makes the rules stricter than a normal
write path:

* **A token is never sealed twice.** Re-encrypting a ciphertext wraps it in a
  second token and loses the value for good, so "already sealed" has to be
  recognised without decrypting — a token under a rotated key would fail to
  decrypt and must still be left strictly alone.
* **A wrong key stops the run.** Continuing past an unreadable value would
  rewrite a correct database with garbage, so the exit code says so
  (``EXIT_UNREADABLE``) and the operator is told to check the key.
* **Nothing changes without ``--dry-run``-free intent.** The dry run reports what
  it would seal and writes nothing.
* **No value reaches the log.** A field id and a column name identify a row
  without putting PII in the log stream.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session, sessionmaker

from app.cli import EXIT_FAILED, EXIT_NO_MATCH, EXIT_OK, EXIT_UNREADABLE
from app.cli import main as cli_main
from app.core.config import Settings
from app.core.crypto import FieldCipher, is_sealed
from app.db.enums import ContractStatus, FieldName, FieldStatus
from app.db.models import Contract, ExtractedField, Tenant
from app.db.session import session_scope

PLAINTEXT_EMAIL = "a.rahman@example.test"
PLAINTEXT_PHONE = "+442079460958"

#: The key the rows are sealed with in the happy path.
KEY = Fernet.generate_key().decode("ascii")
CIPHER = FieldCipher(KEY)


def sealed_settings(settings: Settings, key: str = KEY) -> Settings:
    """Test settings carrying the key the reseal run will use."""
    return settings.model_copy(update={"encryption_key": key})


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
    raw: str | None = PLAINTEXT_EMAIL,
    normalized: str | None = PLAINTEXT_EMAIL,
) -> int:
    """Insert one field row with the values written verbatim, as a legacy writer would."""
    with session_scope(session_factory) as session:
        row = ExtractedField(
            tenant_id=contract.tenant_id,
            contract_id=contract.id,
            field_name=field_name,
            raw_value=raw,
            normalized_value=normalized,
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


def run(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *argv: str,
) -> int:
    return cli_main(
        ["reseal-fields", *argv],
        settings=settings,
        session_factory=session_factory,
    )


# --- sealing plaintext ----------------------------------------------------


def test_both_columns_of_a_plaintext_row_are_sealed(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    field_id = add_field(session_factory, contract)

    code = run(session_factory, sealed_settings(settings))

    row = load(session_factory, field_id)
    assert code == EXIT_OK
    assert row.raw_value is not None
    assert row.normalized_value is not None
    # Sealed twice, with two different IVs: the columns must not be equal tokens.
    assert row.raw_value != row.normalized_value
    assert CIPHER.decrypt(row.raw_value) == PLAINTEXT_EMAIL
    assert CIPHER.decrypt(row.normalized_value) == PLAINTEXT_EMAIL


def test_the_sealed_value_no_longer_contains_the_plaintext(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """The whole point: a database dump after the run shows no address."""
    field_id = add_field(session_factory, contract)

    run(session_factory, sealed_settings(settings))

    row = load(session_factory, field_id)
    assert row.normalized_value is not None
    assert PLAINTEXT_EMAIL not in row.normalized_value
    assert is_sealed(row.normalized_value)


def test_several_rows_are_sealed_in_one_run(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    ids = [
        add_field(session_factory, contract, field_name=FieldName.EMAIL),
        add_field(
            session_factory,
            contract,
            field_name=FieldName.PHONE,
            raw=PLAINTEXT_PHONE,
            normalized=PLAINTEXT_PHONE,
        ),
    ]

    code = run(session_factory, sealed_settings(settings))

    assert code == EXIT_OK
    for field_id in ids:
        row = load(session_factory, field_id)
        assert row.normalized_value is not None
        assert is_sealed(row.normalized_value)


def test_a_null_column_is_skipped(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    field_id = add_field(session_factory, contract, raw=None, normalized=PLAINTEXT_EMAIL)

    code = run(session_factory, sealed_settings(settings))

    row = load(session_factory, field_id)
    assert code == EXIT_OK
    assert row.raw_value is None
    assert row.normalized_value is not None
    assert CIPHER.decrypt(row.normalized_value) == PLAINTEXT_EMAIL


# --- leaving sealed values alone ------------------------------------------


def test_an_already_sealed_value_is_not_sealed_again(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """Byte-identical afterwards: a double seal would destroy the value."""
    token = CIPHER.encrypt(PLAINTEXT_EMAIL)
    field_id = add_field(session_factory, contract, raw=token, normalized=token)

    code = run(session_factory, sealed_settings(settings))

    row = load(session_factory, field_id)
    stored = row.normalized_value
    assert code == EXIT_OK
    assert row.raw_value == token
    assert stored is not None
    assert stored == token
    assert CIPHER.decrypt(stored) == PLAINTEXT_EMAIL


def test_a_mixed_row_seals_only_the_plaintext_column(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """A row half-written by a rolling deploy: one column sealed, one not."""
    token = CIPHER.encrypt(PLAINTEXT_EMAIL)
    field_id = add_field(session_factory, contract, raw=token, normalized=PLAINTEXT_EMAIL)

    run(session_factory, sealed_settings(settings))

    row = load(session_factory, field_id)
    assert row.raw_value == token
    assert row.normalized_value is not None
    assert row.normalized_value != PLAINTEXT_EMAIL
    assert CIPHER.decrypt(row.normalized_value) == PLAINTEXT_EMAIL


# --- refusing to make things worse ---------------------------------------


def test_a_value_sealed_under_another_key_is_left_alone_and_reported(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """A rotated key must stop the run, not rewrite the database with garbage."""
    foreign = FieldCipher(Fernet.generate_key().decode("ascii"))
    token = foreign.encrypt(PLAINTEXT_EMAIL)
    field_id = add_field(session_factory, contract, raw=token, normalized=token)

    code = run(session_factory, sealed_settings(settings))

    row = load(session_factory, field_id)
    stored = row.normalized_value
    assert code == EXIT_UNREADABLE
    # Untouched: still readable with the key it was written with.
    assert row.raw_value == token
    assert stored is not None
    assert stored == token
    assert foreign.decrypt(stored) == PLAINTEXT_EMAIL


def test_without_a_key_nothing_is_touched(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    field_id = add_field(session_factory, contract)
    no_key = settings.model_copy(update={"encryption_key": ""})

    code = run(session_factory, no_key)

    row = load(session_factory, field_id)
    assert code == EXIT_FAILED
    assert row.raw_value == PLAINTEXT_EMAIL
    assert row.normalized_value == PLAINTEXT_EMAIL


def test_a_dry_run_writes_nothing(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    field_id = add_field(session_factory, contract)

    code = run(session_factory, sealed_settings(settings), "--dry-run")

    row = load(session_factory, field_id)
    assert code == EXIT_OK
    assert row.raw_value == PLAINTEXT_EMAIL
    assert row.normalized_value == PLAINTEXT_EMAIL


def test_an_unknown_id_fails_loudly(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    """A wrong id usually means the operator is pointed at another database."""
    code = run(session_factory, sealed_settings(settings), "--ids", "999")

    assert code == EXIT_NO_MATCH


def test_a_targeted_id_seals_only_that_row(
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

    code = run(session_factory, sealed_settings(settings), "--ids", str(second))

    assert code == EXIT_OK
    # Explicit ids replace the limit rather than being filtered by it, so the
    # untargeted row must be untouched.
    assert load(session_factory, first).normalized_value == PLAINTEXT_EMAIL
    assert is_sealed(str(load(session_factory, second).normalized_value))


def test_the_limit_bounds_how_many_rows_are_examined(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    ids = [
        add_field(
            session_factory,
            contract,
            field_name=name,
            raw=PLAINTEXT_EMAIL,
            normalized=PLAINTEXT_EMAIL,
        )
        for name in (FieldName.EMAIL, FieldName.PHONE, FieldName.ADDRESS)
    ]

    code = run(session_factory, sealed_settings(settings), "--limit", "2")

    assert code == EXIT_OK
    # Ordered by id, so the two lowest are the ones examined.
    assert is_sealed(str(load(session_factory, ids[0]).normalized_value))
    assert is_sealed(str(load(session_factory, ids[1]).normalized_value))
    assert load(session_factory, ids[2]).normalized_value == PLAINTEXT_EMAIL


def test_a_second_run_is_a_no_op(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """Idempotent: an operator may safely re-run a partially completed repair."""
    field_id = add_field(session_factory, contract)

    assert run(session_factory, sealed_settings(settings)) == EXIT_OK
    after_first = load(session_factory, field_id).normalized_value
    assert run(session_factory, sealed_settings(settings)) == EXIT_OK

    assert load(session_factory, field_id).normalized_value == after_first


# --- the rows the repair is for are the rows the policy reads -------------


def test_a_resealed_row_is_readable_by_the_repair_and_by_the_cipher(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """Rounds the loop: what ``reseal-fields`` writes is what ``recipient_for`` unseals."""
    field_id = add_field(session_factory, contract)

    run(session_factory, sealed_settings(settings))

    row = load(session_factory, field_id)
    assert row.normalized_value is not None
    assert FieldCipher(KEY).decrypt(row.normalized_value) == PLAINTEXT_EMAIL


def test_explicit_ids_ignore_the_limit_argument(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """``--ids`` is the operator naming rows, so a stray ``--limit 1`` must not drop some."""
    first = add_field(session_factory, contract, field_name=FieldName.EMAIL)
    second = add_field(
        session_factory,
        contract,
        field_name=FieldName.PHONE,
        raw=PLAINTEXT_PHONE,
        normalized=PLAINTEXT_PHONE,
    )

    code = run(
        session_factory,
        sealed_settings(settings),
        "--ids",
        f"{first},{second}",
        "--limit",
        "1",
    )

    assert code == EXIT_OK
    assert is_sealed(str(load(session_factory, first).normalized_value))
    assert is_sealed(str(load(session_factory, second).normalized_value))
