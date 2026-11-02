"""Tenant-scoped repository base.

The isolation rule from the specification is that tenant ownership is enforced
in the service/repository layer, not the UI. This class is that enforcement: it
refuses to be constructed without a tenant, and every query it builds carries
the tenant predicate. A caller that forgets to scope therefore gets an exception
at construction time rather than cross-tenant data.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.db.base import Base


class MissingTenantContextError(RuntimeError):
    """Raised when a tenant-scoped query is attempted with no tenant.

    This is a programming error, not a user error: it means a code path reached
    the data layer without an authenticated principal. Failing loudly is the
    only safe option — falling back to "all tenants" would be a data breach.
    """


class TenantScopedRepository[ModelT: Base]:
    """Read/write access to tenant-owned rows for exactly one tenant.

    ``ModelT`` must be a tenant-owned model, i.e. one that carries ``tenant_id``
    (all of them do via ``TenantMixin``). ``Base`` alone does not declare that
    column, so the tenant predicate is applied through an explicit cast rather
    than a hypothetical attribute mypy cannot see.
    """

    def __init__(self, session: Session, tenant_id: uuid.UUID | None) -> None:
        if tenant_id is None:
            raise MissingTenantContextError(
                "tenant_id is required for a tenant-scoped query; derive it from the "
                "authenticated principal, never from a request body"
            )
        self._session = session
        self._tenant_id = tenant_id

    @property
    def session(self) -> Session:
        return self._session

    @property
    def tenant_id(self) -> uuid.UUID:
        return self._tenant_id

    def scoped(self, model: type[ModelT]) -> Select[tuple[ModelT]]:
        """A ``SELECT`` restricted to this tenant's rows.

        Subclasses and services must build on this rather than on ``select()``
        directly, so there is exactly one place that can get scoping wrong.
        """
        return select(model).where(cast(Any, model).tenant_id == self._tenant_id)

    def get(self, model: type[ModelT], entity_id: object) -> ModelT | None:
        """Fetch one row by primary key, or ``None`` if it belongs to someone else.

        Returning ``None`` (rather than fetching then comparing the tenant) means
        the API answers 404 for another tenant's id and cannot be used to probe
        which ids exist — the IDOR/BOLA case from the specification.
        """
        stmt = self.scoped(model).where(cast(Any, model).id == entity_id)
        return self._session.execute(stmt).scalar_one_or_none()

    def add(self, instance: ModelT) -> ModelT:
        """Persist a row after asserting it belongs to this tenant."""
        row = cast(Any, instance)
        if row.tenant_id is None:
            row.tenant_id = self._tenant_id
        elif row.tenant_id != self._tenant_id:
            raise MissingTenantContextError(
                "refusing to persist a row belonging to a different tenant"
            )
        self._session.add(instance)
        return instance

    def delete(self, instance: ModelT) -> None:
        """Delete a row after asserting it belongs to this tenant."""
        if cast(Any, instance).tenant_id != self._tenant_id:
            raise MissingTenantContextError(
                "refusing to delete a row belonging to a different tenant"
            )
        self._session.delete(instance)
