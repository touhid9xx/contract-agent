"""The attention report over HTTP: one organisation's own board (gap 22).

:mod:`app.core.attention` has answered "what needs a human?" since ``D10-39``, but
only for a reader with no principal — ``python -m app.cli doctor`` and
:class:`~app.workers.attention_watch.AttentionWatch`. Gap 22 was that a tenant had
no way to ask the same question about *their own* rows: the three repository
functions behind the operator reading scan every tenant at once, so serving them
behind a route would have published one organisation's counts and field ids to
another. (Gap 25 is a different question and is not touched here — it is the
deployment default ``EVENT_TRANSPORT=none`` parking every event, which no route
addresses.)

The resolution this module implements is that those two are **different questions**,
not one question with two callers:

* the operator reading asks "is anything anywhere on this deployment stuck?" — it
  has no tenant, so it cannot be narrowed, and its docstrings are right that
  narrowing it would "report a clean board while another organisation's sends sat
  parked";
* this route asks "is anything on *my* board stuck?" — a reader the operator
  docstrings never describe, because it did not exist when they were written.

So :func:`~app.core.attention.tenant_attention_report` sits *beside*
``attention_report`` rather than replacing or narrowing it. The operator functions
are untouched, ``doctor`` still exits on the same numbers, and the watch still logs
the same line.

**Two counts, not three.** ``outbox_parked`` is deliberately absent. A parked event
is a relay fault attributable to the deployment — the tenant cannot fix it, cannot
cause it, and a count on their board would produce a support ticket about somebody
else's misconfigured transport. ``D10-39`` reached the same conclusion from the other
side: it explains parks away under ``EVENT_TRANSPORT=none`` rather than alarming on
them. The two counts that remain are the two a tenant can act on from screens they
already have: a notification that ended ``failed`` or ``suppressed`` is retried from
their notification queue, and a plaintext field value is sealed from their own data.

**Admin only.** This is an operational view whose row-level detail is a list of
field primary keys. Those keys are harmless in themselves — an admin of that tenant
can already open every one of those fields in the review queue — but they are not a
viewer's business, and ``unsealed_field_values`` is a statement about the
deployment's encryption hygiene rather than about a customer. The narrowest role
that can act on the answer is the one that gets it.

**What this route cannot leak.** It takes no tenant from the request: not a query
parameter, not a body field, not a header. ``principal.tenant_id`` is resolved from
the bearer token and a database read in
:func:`~app.api.deps.get_current_principal`, so the only tenant a caller can ask
about is the one they authenticated as. ``backend/tests/test_attention_route.py``
pins that directly: two organisations, each with rows of both kinds, each admin
seeing only their own counts and only their own field ids.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import app_settings, get_request_session, require_roles
from app.core.attention import TenantAttentionReport, tenant_attention_report
from app.core.config import Settings
from app.core.principal import Principal
from app.db.enums import UserRole

router = APIRouter(prefix="/attention", tags=["attention"])


class AttentionView(BaseModel):
    """One organisation's attention reading, as it goes on the wire.

    No ``trace_id``-style metadata and no ``tenant_id`` echo: the caller already knows
    which organisation they are, and echoing it back invites a client to start
    trusting a field that the server never reads from the client.
    """

    notifications_failed: int = Field(
        description="Notifications that ended `failed` — a send the transport refused.",
    )
    notifications_suppressed: int = Field(
        description="Notifications that ended `suppressed` — a policy gate refused.",
    )
    unsealed_field_values: int = Field(
        description=(
            "Extracted-field rows still holding a value in the clear. Counts rows, "
            "not columns: a row with both columns unsealed is one value here."
        ),
    )
    unsealed_field_ids: list[int] = Field(
        description=(
            "Ids of those rows, so a caller can act on them. Ids only — no value is "
            "ever returned by this route, sealing or otherwise."
        ),
    )
    fields_scanned: int = Field(
        description="Rows examined by the unsealed scan, bounded by the configured limit.",
    )
    field_scan_truncated: bool = Field(
        description=(
            "True when the scan filled its page, making `unsealed_field_values` a "
            "floor rather than a total. Never read the count without this flag."
        ),
    )
    needs_attention: bool = Field(
        description="Whether any count above is non-zero, i.e. somebody can act.",
    )


def _view(report: TenantAttentionReport) -> AttentionView:
    return AttentionView(
        notifications_failed=report.notifications_failed,
        notifications_suppressed=report.notifications_suppressed,
        unsealed_field_values=report.unsealed_field_values,
        unsealed_field_ids=list(report.unsealed_field_ids),
        fields_scanned=report.fields_scanned,
        field_scan_truncated=report.field_scan_truncated,
        needs_attention=report.needs_attention,
    )


@router.get(
    "",
    response_model=AttentionView,
    summary="Read this tenant's own attention states",
)
def read_attention(
    principal: Principal = Depends(require_roles(UserRole.ADMIN)),
    session: Session = Depends(get_request_session),
    settings: Settings = Depends(app_settings),
) -> AttentionView:
    """The states on this organisation's board that need a person.

    Read-only, and idempotent in the strict sense: it opens no transaction of its own,
    writes nothing, and takes no lock, so two operators refreshing the screen see the
    same answer twice rather than perturbing each other.

    A clean board is ``200`` with every count zero and ``needs_attention: false`` —
    not an error. The operator reading has the same property, which is why
    ``doctor`` exits ``0`` on a clean database; "nothing is wrong" is an answer.
    """
    report = tenant_attention_report(
        session,
        tenant_id=principal.tenant_id,
        limit=settings.attention_field_scan_limit,
    )
    return _view(report)
