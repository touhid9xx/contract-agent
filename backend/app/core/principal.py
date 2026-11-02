"""The authenticated principal — the only legitimate source of a tenant id.

Everything downstream of the HTTP layer asks this object who it is working for,
rather than accepting a ``tenant_id`` from a request body, query string or
header. That is the rule the specification states as *"tenant identity comes
from the authenticated principal, never from the client"*, and it is enforced by
making this the only type that carries an already-validated tenant.

The values here come from the **database row** re-read on every request, not
from the token's claims. A token is evidence that a principal authenticated at
some point; it is not evidence that they are still active, still in the same
tenant, or not revoked. See ``app.api.deps.get_current_principal``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.db.enums import UserRole


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated user, resolved against the database for this request."""

    user_id: uuid.UUID
    tenant_id: uuid.UUID
    tenant_slug: str
    email: str
    role: UserRole
    #: The epoch the presented token was minted under. Equal to the user's
    #: current ``token_epoch`` by construction; kept so a caller can compare
    #: without another query.
    token_epoch: int

    def has_role(self, *roles: UserRole) -> bool:
        """Whether this principal holds any of ``roles``."""
        return self.role in roles

    @property
    def is_admin(self) -> bool:
        return self.role is UserRole.ADMIN
