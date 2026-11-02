"""ORM behaviour: optimistic locking, tenant-scoped uniqueness, cascade delete."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.db.enums import FieldName, FieldStatus, UserRole
from app.db.models import Contract, ExtractedField, Tenant, User


def make_contract(tenant: Tenant, **overrides: object) -> Contract:
    values: dict[str, object] = {
        "tenant_id": tenant.id,
        "customer_ref": "A. Rahman",
        "end_date": date(2025, 7, 8),
    }
    values.update(overrides)
    return Contract(**values)


def make_field(tenant: Tenant, contract: Contract, **overrides: object) -> ExtractedField:
    values: dict[str, object] = {
        "tenant_id": tenant.id,
        "contract_id": contract.id,
        "field_name": FieldName.EMAIL,
        "raw_value": "a.r@mail.test",
        "confidence": 0.96,
        "status": FieldStatus.AUTO_ACCEPTED,
    }
    values.update(overrides)
    return ExtractedField(**values)


def test_contract_persists_with_defaults(db_session: Session, tenant: Tenant) -> None:
    contract = make_contract(tenant)
    db_session.add(contract)
    db_session.commit()

    assert contract.id is not None
    assert contract.version == 1
    assert contract.status.value == "active"
    assert contract.timezone == "UTC"
    assert contract.created_at is not None


def test_optimistic_lock_rejects_stale_write(db_session: Session, tenant: Tenant) -> None:
    contract = make_contract(tenant)
    db_session.add(contract)
    db_session.commit()
    assert contract.version == 1

    # Simulate a competing writer that already committed version 2 while this
    # session still believes it holds version 1.
    db_session.execute(
        update(Contract)
        .where(Contract.id == contract.id)
        .values(version=2)
        .execution_options(synchronize_session=False)
    )
    contract.customer_ref = "B. Hossain"

    with pytest.raises(StaleDataError):
        db_session.commit()
    db_session.rollback()


def test_sha256_is_unique_per_tenant(db_session: Session, tenant: Tenant) -> None:
    db_session.add(make_contract(tenant, sha256="a" * 64))
    db_session.commit()

    db_session.add(make_contract(tenant, sha256="a" * 64, customer_ref="S. Chowdhury"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_same_sha256_is_allowed_across_tenants(
    db_session: Session, tenant: Tenant, other_tenant: Tenant
) -> None:
    db_session.add(make_contract(tenant, sha256="b" * 64))
    db_session.add(make_contract(other_tenant, sha256="b" * 64))
    db_session.commit()

    total = db_session.execute(select(func.count()).select_from(Contract)).scalar_one()
    assert total == 2


def test_deleting_a_contract_removes_its_fields(db_session: Session, tenant: Tenant) -> None:
    contract = make_contract(tenant)
    db_session.add(contract)
    db_session.commit()

    db_session.add(make_field(tenant, contract))
    db_session.add(make_field(tenant, contract, field_name=FieldName.PHONE, raw_value="+8801"))
    db_session.commit()

    db_session.delete(contract)
    db_session.commit()

    remaining = db_session.execute(select(func.count()).select_from(ExtractedField)).scalar_one()
    assert remaining == 0


def test_one_row_per_field_per_contract(db_session: Session, tenant: Tenant) -> None:
    contract = make_contract(tenant)
    db_session.add(contract)
    db_session.commit()

    db_session.add(make_field(tenant, contract))
    db_session.commit()

    db_session.add(make_field(tenant, contract, raw_value="other@mail.test"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.parametrize("status", [FieldStatus.PENDING_REVIEW, FieldStatus.REJECTED])
def test_pending_and_rejected_fields_block_the_pipeline(
    db_session: Session, tenant: Tenant, status: FieldStatus
) -> None:
    contract = make_contract(tenant)
    db_session.add(contract)
    db_session.commit()

    field = make_field(tenant, contract, status=status, confidence=0.54)
    db_session.add(field)
    db_session.commit()

    assert field.blocks_pipeline is True


@pytest.mark.parametrize("status", [FieldStatus.APPROVED, FieldStatus.AUTO_ACCEPTED])
def test_accepted_fields_do_not_block_the_pipeline(
    db_session: Session, tenant: Tenant, status: FieldStatus
) -> None:
    contract = make_contract(tenant)
    db_session.add(contract)
    db_session.commit()

    field = make_field(tenant, contract, status=status)
    db_session.add(field)
    db_session.commit()

    assert field.blocks_pipeline is False


def test_email_is_unique_per_tenant_not_globally(
    db_session: Session, tenant: Tenant, other_tenant: Tenant
) -> None:
    db_session.add(
        User(
            tenant_id=tenant.id,
            email="op@acme.test",
            password_hash="x",
            role=UserRole.OPERATOR,
        )
    )
    db_session.commit()

    # Same address in a different tenant is fine.
    db_session.add(
        User(
            tenant_id=other_tenant.id,
            email="op@acme.test",
            password_hash="x",
            role="operator",
        )
    )
    db_session.commit()

    # Same address in the same tenant is not.
    db_session.add(
        User(
            tenant_id=tenant.id,
            email="op@acme.test",
            password_hash="y",
            role=UserRole.VIEWER,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
