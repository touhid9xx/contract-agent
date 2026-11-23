"""The portfolio dashboard: one organisation's own totals, counted by the server.

Until this route, no screen in the product could answer "how much of my portfolio
is about to lapse". Every read that looked like it might was a **page**: the
contracts listing returned ``count = len(items)``, the review queue said so in as
many words, and the notifications count is the size of the response. A screen
built from those would have printed "12 contracts" and meant "12 rows", and the
first person to notice would be the one whose portfolio is nine thousand.

So the numbers here are ``COUNT``\\ s over the same predicates the queue routes
filter on, not the lengths of pages — and where a page is shown anyway
(``recent_contracts``) it is labelled as recent activity rather than offered as a
total. That distinction is the whole reason this module exists; the frontend could
not have fixed it with better wording, because a page length is not a wrong label,
it is a different quantity.

**The reminder windows are the planner's own ladder, not a second one.** The demo
dashboard drew a panel of contracts bucketed by days-to-expiry, and the first
version of this route left it out: bucketing by milestone day was a query nothing
in the codebase made, and a band drawn from numbers invented here could have
disagreed with the cadence the planner actually schedules on. The panel is served
now because the bands are **derived from**
:data:`~app.workers.dispatch.DEFAULT_MILESTONE_DAYS` rather than restated — the
same tuple :func:`~app.workers.dispatch.milestone_for` picks a reminder from — so a
change to the cadence moves the panel with it. Each band counts the contracts whose
:func:`milestone_for` answer *today* is that milestone; the bands abut, so a
contract appears in exactly one, and together they cover ``today`` through the
loosest milestone.

**The channel mix is a count of contacts, over a stated window.** The demo drew
this panel as a share of the whole, and that is the shape this route declines to
reproduce: a percentage of a subset invites the reader to treat the remaining share
as "nobody", when it is really "not yet" and "failed". So the panel carries
**counts** — one per channel, zero-filled — plus the two days it counted between, on
the same principle as ``reminder_windows`` and the expiring window. What is counted
is contact that happened: rows in
``CONTACTED_STATUSES``, windowed on ``sent_at``. A ``failed`` or ``suppressed``
row is deliberately outside the total, because counting it would claim a
conversation that never took place; the notification queue and the attention board
are where those rows answer for themselves.

**Admin, operator and viewer.** Unlike :mod:`app.api.v1.attention`, nothing here
is a list of primary keys or a statement about encryption hygiene — every field is
a count of rows the caller can already open from a screen they have. The narrowest
role that can act on the answer is therefore not the only role that may see it, and
a viewer who cannot act is still a viewer who should know the portfolio is not on
fire.

**Which day it is.** ``today`` and ``window_end`` are **UTC calendar dates**, and
they are returned in the response rather than left implicit. The contracts table
carries a per-contract ``timezone`` column, and the expiry sweep resolves the
customer's own day per contract before deciding anything (D2-01) — a ``COUNT``
cannot do that, so this route answers the weaker question and says so. Stating the
window it used is what makes the number checkable: a caller near midnight in Dhaka
sees a window that starts on the previous UTC day rather than a number that
disagrees with nothing they can see.

**What this route cannot leak.** It takes no tenant from the request: not a query
parameter, not a body field, not a header. ``principal.tenant_id`` comes from the
bearer token and a database read in :func:`~app.api.deps.get_current_principal`,
and every repository below is constructed with it, so the only organisation a
caller can ask about is the one they authenticated as.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_request_session, require_roles
from app.api.v1.contracts import ContractListItem, list_item
from app.core.principal import Principal
from app.db.base import utc_now
from app.db.enums import Channel, ContractStatus, UserRole
from app.db.repositories.contracts import ContractRepository
from app.db.repositories.escalations import (
    EscalationRepository,
    overdue_escalation_count_for_tenant,
)
from app.db.repositories.fields import ExtractedFieldRepository
from app.db.repositories.notifications import ALL_CHANNELS, channel_mix_counts_for_tenant
from app.workers.dispatch import DEFAULT_MILESTONE_DAYS

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

#: How far ahead "expiring" looks. Thirty days is the horizon the spec's reminder
#: ladder is built around, and the demo dashboard's headline used the same figure;
#: it is a module constant rather than a setting because a deployment that changed
#: it would change what a number on a screen means without changing its label.
DEFAULT_EXPIRING_WINDOW_DAYS = 30

#: How many recently ingested contracts the activity panel gets. Bounded here for
#: the reason every other listing is bounded at its query: a dashboard is a glance,
#: and a caller cannot ask this route for a tenant's whole contract table.
RECENT_CONTRACT_LIMIT = 5

#: How far back the channel mix looks. **Not** ``DEFAULT_EXPIRING_WINDOW_DAYS``,
#: even though both are thirty: those two numbers answer opposite questions — one
#: a forward horizon for terms about to lapse, this one a trailing window over
#: sends that already happened — and tying them would mean a future change to the
#: reminder horizon silently rewrote what "the last month of contact" means. They
#: are equal today because a month is a month, not because they are one figure.
DEFAULT_CHANNEL_MIX_WINDOW_DAYS = 30


def reminder_window_bands(*, today: date) -> list[tuple[str, int, date, date]]:
    """The planner's milestone ladder as calendar bands, loosest first.

    One tuple per milestone: ``(name, days, start, end)``. The bands **abut and do
    not overlap** — the next-tighter milestone's day count plus one is this band's
    first day — so a contract whose term ends inside the ladder belongs to exactly
    one band. That is the property that makes the panel's numbers safe to read
    together and the reason the boundaries are computed here rather than written
    down as three literal ranges: a fourth rung added to
    :data:`~app.workers.dispatch.DEFAULT_MILESTONE_DAYS` shifts its neighbours
    instead of silently creating a gap or a double count.

    The tightest band starts at ``today``, so a term that has already passed its
    end date is in no band. Those rows are the expiry sweep's business, and
    counting them as "due a T-14 reminder" would offer a reminder nobody can send.
    """
    bands: list[tuple[str, int, date, date]] = []
    for index, (milestone, days) in enumerate(DEFAULT_MILESTONE_DAYS):
        tighter = (
            DEFAULT_MILESTONE_DAYS[index + 1][1] if index + 1 < len(DEFAULT_MILESTONE_DAYS) else -1
        )
        start = today + timedelta(days=tighter + 1)
        bands.append((milestone, days, start, today + timedelta(days=days)))
    return bands


class ReminderWindow(BaseModel):
    """One rung of the reminder ladder, and how many contracts fall in it.

    The days travel with the count for the same reason the expiring window does: a
    name like ``T-60`` is the planner's vocabulary, and a reader who does not know
    what it means would otherwise be looking at a label with no date behind it.
    """

    milestone: str = Field(
        description=(
            "The milestone's name, exactly as the dispatch policy writes it "
            "(`T-60`, `T-30`, `T-14`). Not a display string."
        ),
    )
    days: int = Field(
        description=(
            "Days before the term ends that this milestone falls due — the figure "
            "in the name, kept apart from it so a client never parses a label."
        ),
    )
    start: date = Field(
        description=(
            "First day counted, inclusive, as a UTC calendar date. The day after "
            "the next-tighter milestone's window closes."
        ),
    )
    end: date = Field(
        description=(
            "Last day counted, inclusive: `today` plus `days`, which is the day "
            "this milestone comes due for the term that ends there."
        ),
    )
    contracts: int = Field(
        description=(
            "Contracts whose term ends inside `[start, end]`, restricted to the "
            "states the planner would still remind — the same predicate as "
            "`contracts_expiring`. A contract in this band is one whose reminder "
            "for `milestone` is due today."
        ),
    )


class ChannelMix(BaseModel):
    """One channel, and how many times it actually reached somebody.

    A count rather than a share, and the difference is not cosmetic. A percentage
    has to be a percentage *of* something, and the only honest denominator here is
    all the contact that could have happened — which includes the rows that failed.
    Presenting ``email: 60%`` would leave a reader to guess what filled the other
    forty, and the demo's answer, "channels we did not use", is the least likely of
    the three. A raw count cannot be misread that way: it says how many, and the
    window beside it says when.
    """

    channel: Channel = Field(
        description=(
            "The channel contacted on — a stored enum value, usable as a lookup "
            "key. Present even when `contacts` is zero."
        ),
    )
    contacts: int = Field(
        description=(
            "Notifications in a `sent`-or-later state on this channel whose "
            "`sent_at` falls in the window, when they were dispatched. Failed and "
            "suppressed rows are excluded: they reached nobody, and claiming "
            "otherwise would make this board disagree with the notification queue."
        ),
    )


class DashboardView(BaseModel):
    """One organisation's portfolio, as it goes on the wire.

    No ``tenant_id`` echo: the caller already knows which organisation they are,
    and echoing it back invites a client to start trusting a field the server
    never reads from the client.
    """

    today: date = Field(
        description=(
            "The UTC calendar day this reading was taken. Stated so a caller can "
            "tell when a stale answer was fetched, and because the expiring window "
            "below is a range of dates rather than a countdown."
        ),
    )
    window_end: date = Field(
        description=(
            "Last day of the expiring window, inclusive: `today` plus the window "
            "length. A contract whose `end_date` falls on this day counts."
        ),
    )
    contracts_total: int = Field(
        description=(
            "Every contract this organisation has, in any state — the sum of "
            "`contracts_by_status`. A tenant-scoped `COUNT`, not a page length."
        ),
    )
    contracts_by_status: dict[ContractStatus, int] = Field(
        description=(
            "One entry per lifecycle state, zero-filled from the enum so a state "
            "nothing is in is present as `0` rather than missing. Adding up every "
            "value gives `contracts_total`."
        ),
    )
    contracts_expiring: int = Field(
        description=(
            "Contracts whose term ends between `today` and `window_end`, restricted "
            "to states where a lapse is still nobody's recorded outcome. A "
            "`responded`, `renewed` or `escalated` contract is not counted, because "
            "somebody already acted on it."
        ),
    )
    fields_awaiting_review: int = Field(
        description=(
            "Extracted fields still in `pending_review` — the backlog the review "
            "queue works through. Counted with a `COUNT`, because the queue itself "
            "is paginated and its length is the page size."
        ),
    )
    escalations_open: int = Field(
        description=(
            "Escalations nobody has resolved yet, in any of the three open states. "
            "Includes the overdue ones counted below."
        ),
    )
    escalations_overdue: int = Field(
        description=(
            "Open escalations whose SLA deadline has passed. A subset of "
            "`escalations_open`, never a peer of it — adding the two would count a "
            "late escalation twice."
        ),
    )
    reminder_windows: list[ReminderWindow] = Field(
        description=(
            "The reminder ladder, one entry per milestone, loosest first. Together "
            "the bands are a partition of the terms ending between `today` and the "
            "loosest milestone, so adding up `contracts` counts no contract twice — "
            "unlike `escalations_overdue`, whose subset relationship forbids summing."
        ),
    )
    channel_mix_since: date = Field(
        description=(
            "First day of the channel-mix window, inclusive, as a UTC calendar "
            "date — the day `DEFAULT_CHANNEL_MIX_WINDOW_DAYS` before `today`. A "
            "trailing window, unlike `today`/`window_end`: this one looks back at "
            "sends that happened. Stated in days rather than instants so two reads "
            "inside one UTC day describe the same window."
        ),
    )
    channel_mix_until: date = Field(
        description=(
            "Last day of the channel-mix window, inclusive: the UTC calendar day "
            "this reading was taken, which is `today`. Both ends are inclusive, so "
            "the window is `DEFAULT_CHANNEL_MIX_WINDOW_DAYS` apart — the mirror "
            "image of `window_end`, which sits the same distance ahead."
        ),
    )
    channel_mix: list[ChannelMix] = Field(
        description=(
            "Contact per channel over `[channel_mix_since, channel_mix_until]`, one "
            "entry per channel in the enum's order, zero-filled. These are counts of "
            "contact that happened, so they sum to the messages delivered in the "
            "window — **not** a share of `contracts_total`, and not comparable to "
            "`contracts_expiring`, whose window points the other way."
        ),
    )
    recent_contracts: list[ContractListItem] = Field(
        description=(
            "The most recently ingested contracts, newest first. Recent activity, "
            "**not** a total: it is one bounded page, and `contracts_total` is the "
            "number to use for that question."
        ),
    )


def _channel_mix(
    session: Session,
    tenant_id: uuid.UUID,
    *,
    since: date,
    until: date,
) -> list[ChannelMix]:
    """The mix as a list, in ``ALL_CHANNELS`` order.

    One grouped `COUNT` over the tenant's own rows rather than one per channel:
    unlike the reminder bands — whose ranges are derived arithmetic over a ladder
    that might gain a rung — the channels are a fixed enum, so ``group_by`` answers
    all of them in a single statement.

    The order comes from ``ALL_CHANNELS`` and not from the mapping the reader
    returns. Those agree today, because that reader fills its keys with
    ``dict.fromkeys(ALL_CHANNELS, 0)``; they would stop agreeing the moment somebody
    rebuilt it differently, and a panel that reordered itself is the kind of change
    nobody notices until a screenshot is compared. Iterating the enum is the version
    that cannot drift.
    """
    counts = channel_mix_counts_for_tenant(session, tenant_id, since=since, until=until)
    return [ChannelMix(channel=channel, contacts=counts[channel]) for channel in ALL_CHANNELS]


@router.get(
    "",
    response_model=DashboardView,
    summary="Read this tenant's portfolio totals",
)
def read_dashboard(
    principal: Principal = Depends(
        require_roles(UserRole.ADMIN, UserRole.OPERATOR, UserRole.VIEWER)
    ),
    session: Session = Depends(get_request_session),
) -> DashboardView:
    """This organisation's counts, plus a handful of recent contracts.

    Read-only and strictly idempotent in the sense that matters: it opens no
    transaction of its own, writes nothing, takes no lock, and every statement is a
    ``COUNT`` against an index or a bounded ordered page. Two operators refreshing
    the screen see the same answer twice rather than perturbing each other.

    An empty portfolio is ``200`` with zeros, not an error — "nothing here yet" is
    an answer, and the frontend renders it as an empty state rather than as a
    failure.
    """
    now = utc_now()
    today = now.date()
    window_end = today + timedelta(days=DEFAULT_EXPIRING_WINDOW_DAYS)
    # A trailing window, and in calendar days rather than instants: `now` moves
    # between two reads, and a payload whose window moves with it is not the same
    # payload twice. Days keep the route's idempotency intact for as long as the UTC
    # day lasts, which is what `today`, `window_end` and the bands already rely on.
    channel_mix_since = today - timedelta(days=DEFAULT_CHANNEL_MIX_WINDOW_DAYS)

    contracts = ContractRepository(session, tenant_id=principal.tenant_id)
    fields = ExtractedFieldRepository(session, tenant_id=principal.tenant_id)
    escalations = EscalationRepository(session, tenant_id=principal.tenant_id)

    by_status = contracts.count_by_status()

    return DashboardView(
        today=today,
        window_end=window_end,
        contracts_total=sum(by_status.values()),
        contracts_by_status=by_status,
        contracts_expiring=contracts.count_expiring(start=today, end=window_end),
        fields_awaiting_review=fields.count_awaiting_review(),
        escalations_open=escalations.count_open(),
        escalations_overdue=overdue_escalation_count_for_tenant(
            session, principal.tenant_id, now=now
        ),
        # One COUNT per band rather than one statement with CASE arithmetic. Each is a
        # range over the tenant's own status slice — `ix_contracts_tenant_status` leads
        # on `(tenant_id, status)` — and the three ranges partition the ladder, so the
        # arithmetic stays in `reminder_window_bands` where a test can check it, rather
        # than being written twice, once in SQL and once in the code that labels it.
        reminder_windows=[
            ReminderWindow(
                milestone=milestone,
                days=days,
                start=start,
                end=end,
                contracts=contracts.count_expiring(start=start, end=end),
            )
            for milestone, days, start, end in reminder_window_bands(today=today)
        ],
        channel_mix_since=channel_mix_since,
        channel_mix_until=today,
        channel_mix=_channel_mix(
            session, principal.tenant_id, since=channel_mix_since, until=today
        ),
        recent_contracts=[list_item(row) for row in contracts.recent(limit=RECENT_CONTRACT_LIMIT)],
    )
