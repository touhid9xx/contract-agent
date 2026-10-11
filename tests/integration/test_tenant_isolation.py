"""Integration — verify tenant filter isolates data between tenants.

This is a critical fail-safe test: if two tenants ever see each other's data,
we have a GDPR-level incident. This test must pass on every commit.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from contract_agent.core.security import hash_password
from contract_agent.logging_config import tenant_id_var
from contract_agent.models.user import User, UserRole

pytestmark = pytest.mark.integration


# ============================================================
# FIXTURE: two users in two tenants
# ============================================================
@pytest.fixture()
async def two_tenants(db_session: AsyncSession) -> AsyncIterator[dict]:
    """Create 2 users in 2 different tenants."""
    tenant_a = "aaaaaaaa-0000-0000-0000-000000000001"
    tenant_b = "bbbbbbbb-0000-0000-0000-000000000001"

    user_a = User(
        email="a@tenant-a.com",
        hashed_password=hash_password("Sup3rSecret!Pass"),
        role=UserRole.ADMIN,
        tenant_id=tenant_a,
    )
    user_b = User(
        email="b@tenant-b.com",
        hashed_password=hash_password("Sup3rSecret!Pass"),
        role=UserRole.ADMIN,
        tenant_id=tenant_b,
    )
    db_session.add_all([user_a, user_b])
    await db_session.flush()

    yield {
        "tenant_a": tenant_a,
        "tenant_b": tenant_b,
        "user_a": user_a,
        "user_b": user_b,
    }


# ============================================================
# TESTS
# ============================================================
async def test_tenant_filter_isolates_selects(
    db_session: AsyncSession,
    two_tenants: dict,
) -> None:
    """With tenant_id_var set to A, only A's users are visible."""
    tenant_id_var.set(two_tenants["tenant_a"])
    try:
        result = await db_session.execute(select(User))
        users = result.scalars().all()
        emails = {u.email for u in users}
        assert "a@tenant-a.com" in emails
        assert "b@tenant-b.com" not in emails, "TENANT LEAK!"
    finally:
        tenant_id_var.set(None)


async def test_tenant_filter_allows_admin_override(
    db_session: AsyncSession,
    two_tenants: dict,
) -> None:
    """include_all_tenants=True bypasses the filter (admin/reporting)."""
    tenant_id_var.set(two_tenants["tenant_a"])
    try:
        result = await db_session.execute(select(User).execution_options(include_all_tenants=True))
        emails = {u.email for u in result.scalars().all()}
        assert "a@tenant-a.com" in emails
        assert "b@tenant-b.com" in emails
    finally:
        tenant_id_var.set(None)


async def test_tenant_filter_no_context_returns_all(
    db_session: AsyncSession,
    two_tenants: dict,
) -> None:
    """No tenant in context → no filter applied (health checks, migrations)."""
    tenant_id_var.set(None)
    result = await db_session.execute(select(User))
    emails = {u.email for u in result.scalars().all()}
    assert "a@tenant-a.com" in emails
    assert "b@tenant-b.com" in emails
