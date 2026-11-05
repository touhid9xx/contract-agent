"""Repository layer — the only place that should build queries.

Keeping query construction here (rather than in routers or workers) is what
makes tenant scoping enforceable in one place instead of depending on every
call site remembering a ``WHERE tenant_id = ?`` clause.
"""

from __future__ import annotations

from app.db.repositories.agent import AgentDecisionRepository
from app.db.repositories.audit import AuditLogRepository
from app.db.repositories.base import MissingTenantContextError, TenantScopedRepository
from app.db.repositories.contracts import ContractRepository, contract_for_operator
from app.db.repositories.fields import (
    ExtractedFieldRepository,
    ReviewCursor,
    decode_review_cursor,
    encode_review_cursor,
    fields_for_reseal,
)
from app.db.repositories.notifications import (
    DEFAULT_NOTIFICATION_LIMIT,
    MAX_NOTIFICATION_LIMIT,
    NotificationRepository,
)
from app.db.repositories.outbox import (
    DeadLetterReplay,
    OutboxRepository,
    backoff_seconds,
    dead_lettered,
    enqueue_event,
    replay_dead_lettered,
)
from app.db.repositories.users import IdentityDirectory, UserRepository, normalize_email

__all__ = [
    "DEFAULT_NOTIFICATION_LIMIT",
    "MAX_NOTIFICATION_LIMIT",
    "AgentDecisionRepository",
    "AuditLogRepository",
    "ContractRepository",
    "DeadLetterReplay",
    "ExtractedFieldRepository",
    "IdentityDirectory",
    "MissingTenantContextError",
    "NotificationRepository",
    "OutboxRepository",
    "ReviewCursor",
    "TenantScopedRepository",
    "UserRepository",
    "backoff_seconds",
    "contract_for_operator",
    "dead_lettered",
    "decode_review_cursor",
    "encode_review_cursor",
    "enqueue_event",
    "fields_for_reseal",
    "normalize_email",
    "replay_dead_lettered",
]
