"""User and tenant lookups.

There are two access paths here on purpose, and the difference is the whole
point of the module:

``UserRepository``
    Tenant-scoped like every other repository. It can only ever see rows
    belonging to the tenant it was constructed with, which is why an
    authenticated caller can never list or create a user in someone else's
    organisation.

``IdentityDirectory``
    The one deliberate exception to tenant scoping. Authentication happens
    *before* a tenant is known — the caller supplies a tenant slug plus
    credentials — so finding those rows cannot itself be tenant-scoped. Every
    method here is a system-level lookup, it is read-only, and it must stay that
    way: a write method here would be a cross-tenant write primitive.

Both are fed by the caller's request-scoped session; neither commits.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db.enums import UserRole
from app.db.models import Tenant, User
from app.db.repositories.base import TenantScopedRepository


def normalize_email(value: str) -> str:
    """Fold an address to the form used for storage and lookup.

    Applied on both write and read paths, so "Ops@Acme.test " and
    "ops@acme.test" cannot become two accounts in the same tenant.
    """
    return value.strip().lower()


def is_plausible_email(value: str) -> bool:
    """Cheap structural check, applied before an address is stored.

    Not an RFC 5322 parser and not a deliverability check — those belong to the
    email provider. This only rejects input that could never be an address, so a
    typo cannot silently create an account nobody can ever log in as.
    """
    candidate = normalize_email(value)
    if candidate.count("@") != 1 or any(char.isspace() for char in candidate):
        return False
    local, _, domain = candidate.partition("@")
    return bool(local) and "." in domain and not domain.startswith(".") and not domain.endswith(".")


class UserRepository(TenantScopedRepository[User]):
    """Tenant-scoped access to the users of one organisation."""

    def by_email(self, email: str) -> User | None:
        """The user with this address **in this tenant**, or ``None``."""
        stmt = self.scoped(User).where(User.email == normalize_email(email))
        return self.session.execute(stmt).scalar_one_or_none()

    def list_all(self) -> list[User]:
        """Every user of this tenant, ordered so the output is stable."""
        stmt = self.scoped(User).order_by(User.email)
        return list(self.session.execute(stmt).scalars())

    def create(self, *, email: str, password_hash: str, role: UserRole) -> User:
        """Add a user to this tenant.

        ``TenantScopedRepository.add`` stamps ``tenant_id`` from the repository,
        not from the caller, so there is no argument here that could point the
        new row at another organisation.
        """
        user = User(
            email=normalize_email(email),
            password_hash=password_hash,
            role=role,
            is_active=True,
            token_epoch=0,
        )
        return self.add(user)


class IdentityDirectory:
    """Pre-authentication lookups. System-level, read-only, tenant-agnostic."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def tenant_by_slug(self, slug: str) -> Tenant | None:
        """Resolve the tenant a login attempt is aimed at.

        The slug is tenant *selection*, not tenant *authorisation*: it says which
        organisation the credentials should be checked against, and the token
        that follows carries the id of the tenant that actually owned the
        matched user row.
        """
        stmt = select(Tenant).where(Tenant.slug == slug.strip().lower())
        return self._session.execute(stmt).scalar_one_or_none()

    def user_by_id(self, user_id: uuid.UUID) -> User | None:
        """Load a user by primary key, with its tenant eager-loaded.

        Used on every authenticated request. The join is eager because the
        caller needs ``tenant.is_active`` immediately; a lazy load here would be
        an extra round trip per request.
        """
        stmt = select(User).options(joinedload(User.tenant)).where(User.id == user_id)
        return self._session.execute(stmt).scalar_one_or_none()
