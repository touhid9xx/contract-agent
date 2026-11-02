"""Tenant isolation at the data-access layer.

These are the tests the specification calls for directly: tenant A must not be
able to read or mutate tenant B's rows even when it knows the primary key. A
failing one of these is a security defect, not a bug.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.orm import Session

from app.db.models import Contract, Tenant
from app.db.repositories import MissingTenantContextError, TenantScopedRepository


def make_contract(tenant: Tenant, customer_ref: str) -> Contract:
    return Contract(
        tenant_id=tenant.id,
        customer_ref=customer_ref,
        end_date=date(2025, 7, 8),
    )


def contract_repo(session: Session, tenant: Tenant) -> TenantScopedRepository[Contract]:
    """A ``Contract`` repository for ``tenant``.

    The base class is generic, so the model parameter has to be named somewhere;
    doing it once here keeps every test below readable.
    """
    return TenantScopedRepository[Contract](session, tenant.id)


def test_repository_refuses_to_build_without_a_tenant(db_session: Session) -> None:
    with pytest.raises(MissingTenantContextError):
        TenantScopedRepository(db_session, None)


def test_scoped_query_returns_only_own_rows(
    db_session: Session, tenant: Tenant, other_tenant: Tenant
) -> None:
    mine = make_contract(tenant, "A. Rahman")
    theirs = make_contract(other_tenant, "S. Chowdhury")
    db_session.add_all([mine, theirs])
    db_session.commit()

    repository = contract_repo(db_session, tenant)
    rows = list(db_session.execute(repository.scoped(Contract)).scalars())

    assert [row.id for row in rows] == [mine.id]


def test_get_by_id_cannot_reach_another_tenants_row(
    db_session: Session, tenant: Tenant, other_tenant: Tenant
) -> None:
    theirs = make_contract(other_tenant, "S. Chowdhury")
    db_session.add(theirs)
    db_session.commit()

    repository = contract_repo(db_session, tenant)

    # Guessing the id must yield "not found", never data and never a hint that
    # the id exists (IDOR/BOLA).
    assert repository.get(Contract, theirs.id) is None


def test_get_returns_own_row(db_session: Session, tenant: Tenant, other_tenant: Tenant) -> None:
    mine = make_contract(tenant, "A. Rahman")
    db_session.add_all([mine, make_contract(other_tenant, "S. Chowdhury")])
    db_session.commit()

    repository = contract_repo(db_session, tenant)
    found = repository.get(Contract, mine.id)

    assert found is not None
    assert found.customer_ref == "A. Rahman"


def test_add_stamps_the_authenticated_tenant(db_session: Session, tenant: Tenant) -> None:
    repository = contract_repo(db_session, tenant)
    contract = Contract(customer_ref="M. Hossain", end_date=date(2025, 6, 22))

    repository.add(contract)
    db_session.commit()

    assert contract.tenant_id == tenant.id


def test_add_rejects_a_row_for_another_tenant(
    db_session: Session, tenant: Tenant, other_tenant: Tenant
) -> None:
    repository = contract_repo(db_session, tenant)
    foreign = make_contract(other_tenant, "N. Begum")

    with pytest.raises(MissingTenantContextError):
        repository.add(foreign)


def test_delete_rejects_a_row_for_another_tenant(
    db_session: Session, tenant: Tenant, other_tenant: Tenant
) -> None:
    theirs = make_contract(other_tenant, "N. Begum")
    db_session.add(theirs)
    db_session.commit()

    repository = contract_repo(db_session, tenant)

    with pytest.raises(MissingTenantContextError):
        repository.delete(theirs)


def test_delete_removes_own_row(db_session: Session, tenant: Tenant) -> None:
    repository = contract_repo(db_session, tenant)
    contract = make_contract(tenant, "R. Akter")
    repository.add(contract)
    db_session.commit()

    repository.delete(contract)
    db_session.commit()

    assert repository.get(Contract, contract.id) is None
