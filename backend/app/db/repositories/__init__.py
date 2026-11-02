"""Repository layer — the only place that should build queries.

Keeping query construction here (rather than in routers or workers) is what
makes tenant scoping enforceable in one place instead of depending on every
call site remembering a ``WHERE tenant_id = ?`` clause.
"""

from __future__ import annotations

from app.db.repositories.base import MissingTenantContextError, TenantScopedRepository
from app.db.repositories.outbox import OutboxRepository, backoff_seconds, enqueue_event
from app.db.repositories.users import IdentityDirectory, UserRepository, normalize_email

__all__ = [
    "IdentityDirectory",
    "MissingTenantContextError",
    "OutboxRepository",
    "TenantScopedRepository",
    "UserRepository",
    "backoff_seconds",
    "enqueue_event",
    "normalize_email",
]
