"""Append-only audit log (F8).

``AuditLog`` has existed since the F2 schema and had no writer until review
landed: reviewing a field and revealing its value are the first two actions that
change something a human is accountable for, so they are the first two that leave
a trail.

Two rules this module exists to keep true, both stated in the model docstring:

1. **Append-only.** There is no update and no delete here. A log an editor can
   rewrite is not evidence.
2. **No values, ever.** ``before``/``after`` hold JSON snapshots that the *caller*
   built, and the callers in ``app/api/v1/review.py`` put status, version and
   confidence in them — never a phone number or an email address. Storing a
   decrypted value here would defeat the encryption F7 applies to
   ``ExtractedField.raw_value``, because the log is readable by any operator.

The tenant is stamped from the repository, so a caller cannot write one tenant's
audit row onto another's.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from app.db.models import AuditLog
from app.db.repositories.base import TenantScopedRepository

__all__ = ["ACTOR_USER", "AUDIT_ENTITY_CONTRACT", "AUDIT_ENTITY_FIELD", "AuditLogRepository"]

#: ``AuditLog.entity`` for extracted-field rows, matching the table name so the
#: log joins back to what it describes without a lookup table of its own.
AUDIT_ENTITY_FIELD = "extracted_fields"

#: ``AuditLog.entity`` for contract rows. Consent and opt-out are recorded here
#: rather than on a separate consent table: the flags are columns on the contract,
#: and the audit row is what says who set them and when.
AUDIT_ENTITY_CONTRACT = "contracts"

#: ``AuditLog.actor_type`` value for a person acting through the API. The model
#: also allows ``worker``/``system`` for actions that have no human actor.
ACTOR_USER = "user"


def _snapshot(state: Mapping[str, Any] | None) -> str | None:
    """Serialize a caller-built snapshot, or ``None`` when there is nothing to say.

    ``sort_keys`` so two logically equal snapshots are byte-identical in the log;
    nothing here inspects or redacts, because the caller is responsible for not
    putting a value in (see the module docstring).
    """
    if state is None:
        return None
    return json.dumps(dict(state), sort_keys=True, default=str)


class AuditLogRepository(TenantScopedRepository[AuditLog]):
    """Tenant-scoped writer for the append-only audit trail."""

    def record(
        self,
        *,
        correlation_id: str,
        actor_type: str,
        actor_id: uuid.UUID | None,
        action: str,
        entity: str,
        entity_id: str,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        reason: str | None = None,
    ) -> AuditLog:
        """Append one row describing who did what.

        ``entity_id`` is a stringified primary key rather than an integer foreign
        key on purpose: the log is generic over entities and has to survive a
        schema change to any one of them (see the model).
        """
        row = AuditLog(
            correlation_id=correlation_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            entity=entity,
            entity_id=entity_id,
            before_state=_snapshot(before),
            after_state=_snapshot(after),
            reason=reason,
        )
        return self.add(row)
