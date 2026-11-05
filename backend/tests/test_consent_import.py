"""Bulk consent import (gap 18's buildable half).

The command reads a CSV sheet an operator produced elsewhere and writes the four
consent booleans onto contracts. That makes it a *bulk write to a contact
permission*, so the rules are stricter than a normal write path:

* **Nothing is written unless every line parses.** A half-applied sheet leaves the
  database holding consent nobody can account for — the run refuses instead.
* **A sheet that agrees with the database changes nothing.** No version bump, no
  ``consent_updated_at`` move, no audit row, so re-running an export is a no-op.
* **A named contract that does not exist is reported.** The rest still applies,
  but the exit code refuses to call the run a success.
* **The audit row names no person.** There is no principal behind a CLI run, so
  ``actor_type`` is ``system`` and ``actor_id`` is empty rather than borrowed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.cli import EXIT_FAILED, EXIT_NO_MATCH, EXIT_OK
from app.cli import main as cli_main
from app.core.config import Settings
from app.db.enums import ContractStatus
from app.db.models import AuditLog, Contract, Tenant
from app.db.repositories.audit import ACTION_CONSENT_UPDATED
from app.db.session import session_scope

HEADER = "contract_id,consent_email,consent_sms,consent_voice,do_not_contact"


@pytest.fixture
def contract(session_factory: sessionmaker[Session], tenant: Tenant) -> Contract:
    """One contract with every consent flag at its fail-closed default."""
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


def add_contract(session_factory: sessionmaker[Session], tenant: Tenant, ref: str) -> int:
    """Insert a second contract with a distinguishable label; return its id."""
    with session_scope(session_factory) as session:
        row = Contract(
            tenant_id=tenant.id,
            customer_ref=ref,
            timezone="UTC",
            status=ContractStatus.ACTIVE,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def sheet(tmp_path: Path, *lines: str, header: str = HEADER) -> Path:
    """Write a consent sheet from the given data lines."""
    path = tmp_path / "consent.csv"
    path.write_text("\n".join([header, *lines]) + "\n", encoding="utf-8")
    return path


def load(session_factory: sessionmaker[Session], contract_id: int) -> Contract:
    with session_scope(session_factory) as session:
        row = session.get(Contract, contract_id)
        assert row is not None
        return row


def audit_rows(session_factory: sessionmaker[Session]) -> list[AuditLog]:
    with session_scope(session_factory) as session:
        return list(session.execute(select(AuditLog)).scalars())


def run(session_factory: sessionmaker[Session], settings: Settings, path: Path, *flags: str) -> int:
    return cli_main(
        ["consent-import", "--file", str(path), *flags],
        settings=settings,
        session_factory=session_factory,
    )


# --- applying a sheet -----------------------------------------------------


def test_a_row_sets_all_four_flags(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract, tmp_path: Path
) -> None:
    path = sheet(tmp_path, f"{contract.id},true,false,true,false")

    code = run(session_factory, settings, path)

    row = load(session_factory, contract.id)
    assert code == EXIT_OK
    assert row.consent_email is True
    assert row.consent_sms is False
    assert row.consent_voice is True
    assert row.do_not_contact is False
    assert row.consent_updated_at is not None


def test_the_import_leaves_an_audit_row_naming_the_command(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract, tmp_path: Path
) -> None:
    """``system`` rather than a borrowed user id: no human ran this as a principal."""
    path = sheet(tmp_path, f"{contract.id},true,false,false,false")

    run(session_factory, settings, path)

    rows = audit_rows(session_factory)
    assert len(rows) == 1
    entry = rows[0]
    assert entry.action == ACTION_CONSENT_UPDATED
    assert entry.entity == "contracts"
    assert entry.entity_id == str(contract.id)
    assert entry.actor_type == "system"
    assert entry.actor_id is None
    assert entry.reason is not None
    assert "consent.csv" in entry.reason
    # The snapshot must be free of stored contact values; consent is four booleans.
    assert entry.before_state is not None
    assert "consent_email" in entry.before_state
    assert "@" not in entry.before_state


def test_a_revoking_sheet_turns_consent_off(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract, tmp_path: Path
) -> None:
    """The sheet is the complete state, so all-false revokes what was granted."""
    run(session_factory, settings, sheet(tmp_path, f"{contract.id},true,true,true,false"))
    assert load(session_factory, contract.id).may_contact is True

    revoke = tmp_path / "revoke.csv"
    revoke.write_text(f"{HEADER}\n{contract.id},false,false,false,true\n", encoding="utf-8")
    code = run(session_factory, settings, revoke)

    row = load(session_factory, contract.id)
    assert code == EXIT_OK
    assert not row.consent_channels
    assert row.do_not_contact is True
    assert row.may_contact is False


def test_two_rows_are_applied_in_one_run(
    session_factory: sessionmaker[Session],
    settings: Settings,
    contract: Contract,
    tenant: Tenant,
    tmp_path: Path,
) -> None:
    other = add_contract(session_factory, tenant, "B. Chowdhury")
    path = sheet(
        tmp_path,
        f"{contract.id},true,false,false,false",
        f"{other},false,true,false,false",
    )

    code = run(session_factory, settings, path)

    assert code == EXIT_OK
    assert load(session_factory, contract.id).consent_email is True
    assert load(session_factory, other).consent_sms is True


# --- re-running a sheet ---------------------------------------------------


def test_a_resubmitted_sheet_writes_nothing(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract, tmp_path: Path
) -> None:
    """Idempotent: no version bump, no second audit row, same consent date."""
    path = sheet(tmp_path, f"{contract.id},true,false,false,false")
    run(session_factory, settings, path)
    first = load(session_factory, contract.id)
    stamp = first.consent_updated_at
    version = first.version

    code = run(session_factory, settings, path)

    again = load(session_factory, contract.id)
    assert code == EXIT_OK
    assert again.version == version
    assert again.consent_updated_at == stamp
    assert len(audit_rows(session_factory)) == 1


def test_a_dry_run_reports_the_change_without_making_it(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract, tmp_path: Path
) -> None:
    path = sheet(tmp_path, f"{contract.id},true,false,false,false")

    code = run(session_factory, settings, path, "--dry-run")

    row = load(session_factory, contract.id)
    assert code == EXIT_OK
    assert row.consent_email is False
    assert row.consent_updated_at is None
    assert audit_rows(session_factory) == []


# --- refusing -------------------------------------------------------------


def test_a_malformed_cell_stops_the_whole_import(
    session_factory: sessionmaker[Session],
    settings: Settings,
    contract: Contract,
    tenant: Tenant,
    tmp_path: Path,
) -> None:
    """The good line is *not* applied: half a sheet is worse than none of it."""
    other = add_contract(session_factory, tenant, "B. Chowdhury")
    path = sheet(
        tmp_path,
        f"{contract.id},true,false,false,false",
        f"{other},ture,false,false,false",
    )

    code = run(session_factory, settings, path)

    assert code == EXIT_FAILED
    assert load(session_factory, contract.id).consent_email is False
    assert load(session_factory, other).consent_email is False
    assert audit_rows(session_factory) == []


def test_a_blank_cell_is_an_unanswered_question(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract, tmp_path: Path
) -> None:
    """Not a silent ``false``: defaulting a channel revokes a consent nobody revoked."""
    path = sheet(tmp_path, f"{contract.id},,false,false,false")

    code = run(session_factory, settings, path)

    assert code == EXIT_FAILED
    assert load(session_factory, contract.id).consent_updated_at is None


def test_a_missing_column_is_refused(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract, tmp_path: Path
) -> None:
    path = sheet(
        tmp_path,
        f"{contract.id},true,false,false",
        header="contract_id,consent_email,consent_sms,consent_voice",
    )

    code = run(session_factory, settings, path)

    assert code == EXIT_FAILED
    assert load(session_factory, contract.id).consent_email is False


def test_a_duplicate_contract_id_is_refused(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract, tmp_path: Path
) -> None:
    """Two lines for one contract have no defensible winner, so neither is applied."""
    path = sheet(
        tmp_path,
        f"{contract.id},true,false,false,false",
        f"{contract.id},false,true,false,false",
    )

    code = run(session_factory, settings, path)

    assert code == EXIT_FAILED
    row = load(session_factory, contract.id)
    assert row.consent_email is False
    assert row.consent_sms is False


def test_a_header_only_sheet_is_refused(
    session_factory: sessionmaker[Session], settings: Settings, tmp_path: Path
) -> None:
    """A scheduled import must not report success every night for an empty export."""
    path = sheet(tmp_path)

    assert run(session_factory, settings, path) == EXIT_FAILED


def test_an_unreadable_file_is_refused(
    session_factory: sessionmaker[Session], settings: Settings, tmp_path: Path
) -> None:
    missing = tmp_path / "not-there.csv"

    assert run(session_factory, settings, missing) == EXIT_FAILED


def test_an_unknown_contract_is_reported_but_the_rest_apply(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract, tmp_path: Path
) -> None:
    """A stale id usually means the sheet came from another environment."""
    path = sheet(
        tmp_path,
        f"{contract.id},true,false,false,false",
        "999999,false,false,false,false",
    )

    code = run(session_factory, settings, path)

    assert code == EXIT_NO_MATCH
    assert load(session_factory, contract.id).consent_email is True


# --- sheet formats --------------------------------------------------------


def test_a_spreadsheet_bom_is_tolerated(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract, tmp_path: Path
) -> None:
    """Excel writes a BOM; without ``utf-8-sig`` the header looks misspelled."""
    path = tmp_path / "consent.csv"
    path.write_text(f"{HEADER}\n{contract.id},true,false,false,false\n", encoding="utf-8-sig")

    code = run(session_factory, settings, path)

    assert code == EXIT_OK
    assert load(session_factory, contract.id).consent_email is True


def test_yes_no_and_one_zero_are_accepted(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract, tmp_path: Path
) -> None:
    path = sheet(tmp_path, f"{contract.id},yes,no,1,0")

    code = run(session_factory, settings, path)

    row = load(session_factory, contract.id)
    assert code == EXIT_OK
    assert row.consent_email is True
    assert row.consent_sms is False
    assert row.consent_voice is True
    assert row.do_not_contact is False
