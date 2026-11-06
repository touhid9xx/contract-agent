"""All ORM models, imported here so Alembic sees one complete ``metadata``.

Import order matters only for readability: SQLAlchemy resolves the string-based
foreign keys lazily, so a model may reference a table defined later in this list.
"""

from app.db.models.contract import Contract, ExtractedField
from app.db.models.delivery import AgentDecision, CustomerResponse, Notification
from app.db.models.escalation import AuditLog, Escalation
from app.db.models.outbox import OutboxEvent
from app.db.models.security import LoginAttempt
from app.db.models.tenant import Tenant, User

__all__ = [
    "AgentDecision",
    "AuditLog",
    "Contract",
    "CustomerResponse",
    "Escalation",
    "ExtractedField",
    "LoginAttempt",
    "Notification",
    "OutboxEvent",
    "Tenant",
    "User",
]
