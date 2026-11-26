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
from app.db.repositories.escalations import (
    DEFAULT_ESCALATION_QUEUE_LIMIT,
    DEFAULT_RESPONSE_HISTORY_LIMIT,
    MAX_ESCALATION_QUEUE_LIMIT,
    MAX_RESPONSE_HISTORY_LIMIT,
    CustomerResponseRepository,
    EscalationRepository,
)
from app.db.repositories.fields import (
    ExtractedFieldRepository,
    ReviewCursor,
    decode_review_cursor,
    encode_review_cursor,
    fields_for_reseal,
)
from app.db.repositories.notifications import (
    ALL_CHANNELS,
    CONTACTED_STATUSES,
    DEFAULT_NOTIFICATION_LIMIT,
    MAX_NOTIFICATION_LIMIT,
    UNDELIVERED_STATUSES,
    NotificationRepository,
    channel_mix_counts_for_tenant,
    notification_for_provider_message_id,
    pending_overdue_counts,
    undelivered_counts,
)
from app.db.repositories.outbox import (
    DEFAULT_PRUNE_LIMIT,
    DeadLetterReplay,
    OutboxRepository,
    PrunedEvent,
    backoff_seconds,
    dead_letter_count,
    dead_lettered,
    enqueue_event,
    outbox_pending_overdue_count,
    prune_terminal_before,
    replay_dead_lettered,
)
from app.db.repositories.security import LoginAttemptRepository, RefreshTokenRepository
from app.db.repositories.users import IdentityDirectory, UserRepository, normalize_email

__all__ = [
    "ALL_CHANNELS",
    "CONTACTED_STATUSES",
    "DEFAULT_ESCALATION_QUEUE_LIMIT",
    "DEFAULT_NOTIFICATION_LIMIT",
    "DEFAULT_PRUNE_LIMIT",
    "DEFAULT_RESPONSE_HISTORY_LIMIT",
    "MAX_ESCALATION_QUEUE_LIMIT",
    "MAX_NOTIFICATION_LIMIT",
    "MAX_RESPONSE_HISTORY_LIMIT",
    "UNDELIVERED_STATUSES",
    "AgentDecisionRepository",
    "AuditLogRepository",
    "ContractRepository",
    "CustomerResponseRepository",
    "DeadLetterReplay",
    "EscalationRepository",
    "ExtractedFieldRepository",
    "IdentityDirectory",
    "LoginAttemptRepository",
    "MissingTenantContextError",
    "NotificationRepository",
    "OutboxRepository",
    "PrunedEvent",
    "RefreshTokenRepository",
    "ReviewCursor",
    "TenantScopedRepository",
    "UserRepository",
    "backoff_seconds",
    "channel_mix_counts_for_tenant",
    "contract_for_operator",
    "dead_letter_count",
    "dead_lettered",
    "decode_review_cursor",
    "encode_review_cursor",
    "enqueue_event",
    "fields_for_reseal",
    "normalize_email",
    "notification_for_provider_message_id",
    "outbox_pending_overdue_count",
    "pending_overdue_counts",
    "prune_terminal_before",
    "replay_dead_lettered",
    "undelivered_counts",
]
