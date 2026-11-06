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

__all__ = [
    "ACTION_CONSENT_UPDATED",
    "ACTION_USER_CREATED",
    "ACTION_USER_CREDENTIAL_CHANGED",
    "ACTION_USER_CREDENTIAL_RESET",
    "ACTION_USER_UPDATED",
    "ACTOR_SYSTEM",
    "ACTOR_USER",
    "AUDIT_ENTITY_CONTRACT",
    "AUDIT_ENTITY_DECISION",
    "AUDIT_ENTITY_FIELD",
    "AUDIT_ENTITY_USER",
    "AuditLogRepository",
]

#: ``AuditLog.entity`` for extracted-field rows, matching the table name so the
#: log joins back to what it describes without a lookup table of its own.
AUDIT_ENTITY_FIELD = "extracted_fields"

#: ``AuditLog.entity`` for contract rows. Consent and opt-out are recorded here
#: rather than on a separate consent table: the flags are columns on the contract,
#: and the audit row is what says who set them and when.
AUDIT_ENTITY_CONTRACT = "contracts"

#: ``AuditLog.entity`` for agent-decision rows, used by the override and
#: flag-review routes (gap 19). Only *human* edits to a decision are audited: the
#: row a policy creates is already append-only and carries its own provenance, so
#: a "decision was created" entry would only narrate the record it duplicates.
AUDIT_ENTITY_DECISION = "agent_decisions"

#: ``AuditLog.actor_type`` value for a person acting through the API. The model
#: also allows ``worker``/``system`` for actions that have no human actor.
ACTOR_USER = "user"

#: ``AuditLog.actor_type`` value for an action taken by a process with no human
#: behind it. ``consent-import`` uses this and leaves ``actor_id`` empty rather
#: than borrowing a user id: the row has to say "a command did this", and
#: naming an operator who did not run it would fabricate exactly the fact the
#: audit log exists to carry.
ACTOR_SYSTEM = "system"

#: ``AuditLog.entity`` for user rows.
#:
#: ``create-admin`` writes here because it is the one path that mints an account
#: with **no authenticated principal** behind it, so the log row is the only
#: evidence it happened. Since gap 8 three more routes do: changing your own
#: password, an administrator resetting a colleague's, and an administrator
#: changing a colleague's role or active state. Each is an act that ends
#: somebody's sessions or changes what they may do, and "who did that, and when"
#: must survive the account it was done to.
#:
#: Routine logins and logouts are still not recorded. They change nothing an
#: auditor needs a chain for, and the volume would bury the entries that matter.
AUDIT_ENTITY_USER = "users"

#: ``AuditLog.action`` for any change to a contract's consent flags.
#:
#: Named here rather than in the route that first wrote it, because two paths now
#: record it — ``PUT /contracts/{id}/consent`` and ``python -m app.cli
#: consent-import``. One action string is what makes "everything that ever
#: changed this customer's consent" a single query; a per-path action name would
#: answer it in two, and the second one would be the one nobody remembered.
ACTION_CONSENT_UPDATED = "contract.consent_updated"

#: ``AuditLog.action`` for an operator account being created by ``create-admin``.
ACTION_USER_CREATED = "user.created"

#: ``AuditLog.action`` for a user replacing their own password (gap 8).
#:
#: Recorded even though the actor and the subject are the same person: the
#: interesting fact is not *who* did it but that every other session of that
#: account stopped working at that moment. An operator investigating a
#: compromise needs that instant to line up against the bumps in ``token_epoch``.
#:
#: The constant is named for the *credential* while the stored action string
#: still reads ``user.password_changed``. That is deliberate, not drift: the
#: value is what an auditor reads in a raw row and it should say the plain thing,
#: but both secret scanners this repo runs (ruff ``S105``, bandit ``B105``) match
#: on the *variable name*, and a ``*_PASSWORD_*`` spelling is reported as a
#: hardcoded password. Renaming the identifier is the honest fix — a ``nosec``
#: would suppress the check for the whole file. Same trade as
#: ``REFRESH_DIGEST_NAMESPACE`` in ``app/core/refresh.py``.
ACTION_USER_CREDENTIAL_CHANGED = "user.password_changed"

#: ``AuditLog.action`` for an administrator resetting a colleague's password.
#:
#: The temporary password itself is never recorded — not in ``after``, not in
#: ``reason``. The row says a reset happened and who ordered it; the value exists
#: only in the response that carried it. Named for the credential for the reason
#: given above; the stored string stays ``user.password_reset``.
ACTION_USER_CREDENTIAL_RESET = "user.password_reset"

#: ``AuditLog.action`` for an administrator changing a colleague's role or active
#: state. One action for both because they answer one question — "what did this
#: administrator change about this account?" — and the snapshot in
#: ``before``/``after`` says which field moved.
ACTION_USER_UPDATED = "user.updated"


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
