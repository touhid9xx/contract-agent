"""The dashboard route: a tenant's totals, counted rather than measured off a page.

The route exists because no read in the product could answer "how much of my
portfolio is about to lapse". Every number that looked like it might was a **page
length** — ``ContractListResponse.count`` is ``len(items)``, the review queue says
so in as many words, and ``NotificationListResponse.count`` is the size of the
response. This file pins the properties that make the new numbers different:

* **A count is a count.** More contracts exist than the recent-activity panel
  returns, and ``contracts_total`` still says all of them. That is the whole point
  of the slice: the assertion would pass just as happily if the route returned
  ``len(recent_contracts)``, so it is written so that it cannot.
* **Every state is present.** ``contracts_by_status`` is zero-filled from the
  enum, and the two failure modes are different — a missing key makes a board
  render a gap, a missing zero makes a tenant's rows stop adding up. Both are
  asserted, and the sum is asserted equal to ``contracts_total`` so the two can
  never disagree on screen.
* **Expiring means still nobody's outcome.** A term ending inside the window only
  counts while the contract is in a state where a lapse is still unaddressed; a
  ``responded`` row is excluded, and so is a row whose term never arrived.
* **Overdue is a subset of open, never a peer.** Adding them would count a late
  escalation twice, which is exactly the arithmetic a careless headline performs.
* **The reminder bands partition, so they *may* be summed.** They are drawn from
  ``DEFAULT_MILESTONE_DAYS`` — the tuple the planner itself picks a reminder from —
  and a contract lands in exactly one band, which is the opposite of the
  subset relationship above and is asserted both ways: every boundary row counted
  in exactly one band, and the bands tiling the window from ``today`` to the
  loosest milestone with no gap between them.
* **One organisation's totals reach nobody else.** Two tenants, one busy and one
  clean: the busy one's numbers are unchanged and the clean one's board is zeros
  rather than the neighbour's. The clean side is the assertion that matters — an
  unscoped read that leaked nothing in the busy direction is still a leak.
* **The tenant cannot be asked for.** ``?tenant_id=`` is accepted by FastAPI and
  ignored by the route, which takes its tenant from the token.
* **Reading it changes nothing.** No audit row, no version bump, no lock.

The role rule is asserted once: this view is counts of rows every role can already
open, so a viewer may read it — unlike the attention board, whose payload is a list
of field primary keys.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.v1.dashboard import (
    DEFAULT_CHANNEL_MIX_WINDOW_DAYS,
    DEFAULT_EXPIRING_WINDOW_DAYS,
    RECENT_CONTRACT_LIMIT,
    reminder_window_bands,
)
from app.db.base import utc_now
from app.db.enums import (
    Channel,
    ContractStatus,
    EscalationStatus,
    FieldName,
    FieldStatus,
    NotificationStatus,
    UserRole,
)
from app.db.models import (
    AuditLog,
    Contract,
    Escalation,
    ExtractedField,
    Notification,
    Tenant,
    User,
)
from app.db.repositories.notifications import ALL_CHANNELS
from app.db.session import session_scope
from app.workers.dispatch import DEFAULT_MILESTONE_DAYS

DASHBOARD_PATH = "/api/v1/dashboard"
LOGIN_PATH = "/api/v1/auth/login"

#: Matches ``conftest.DEFAULT_PASSWORD``; protects in-memory databases only.
PASSWORD = "correct-horse-battery-staple"  # nosec B105
EMAIL = "ops@acme.test"


# --- helpers --------------------------------------------------------------
@pytest.fixture
def make_contract(session_factory: sessionmaker[Session]) -> Callable[..., Contract]:
    """Insert one contract for a tenant and hand back the detached row."""

    def _make(
        owner: Tenant,
        *,
        status: ContractStatus = ContractStatus.ACTIVE,
        end_date: date | None = None,
        customer_ref: str = "A. Rahman",
    ) -> Contract:
        with session_scope(session_factory) as session:
            row = Contract(
                tenant_id=owner.id,
                customer_ref=customer_ref,
                timezone="UTC",
                status=status,
                end_date=end_date,
            )
            session.add(row)
            session.flush()
            return row

    return _make


def add_field(
    session_factory: sessionmaker[Session],
    contract: Contract,
    *,
    field_name: FieldName = FieldName.EMAIL,
    status: FieldStatus = FieldStatus.PENDING_REVIEW,
) -> int:
    """One extracted-field row in a chosen review state.

    Written directly rather than through the extractor: which fields get flagged
    is ``test_extraction``'s subject. Here the state is the input, and the question
    is only whether the dashboard counts the row.
    """
    with session_scope(session_factory) as session:
        row = ExtractedField(
            tenant_id=contract.tenant_id,
            contract_id=contract.id,
            field_name=field_name,
            raw_value="a.rahman@example.test",
            normalized_value="a.rahman@example.test",
            confidence=0.41,
            status=status,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def add_escalation(
    session_factory: sessionmaker[Session],
    contract: Contract,
    *,
    status: EscalationStatus = EscalationStatus.OPEN,
    deadline_in: timedelta = timedelta(hours=-1),
) -> int:
    """One escalation, late by default — the state every count here is about."""
    moment = utc_now()
    with session_scope(session_factory) as session:
        row = Escalation(
            correlation_id="corr-dashboard",
            tenant_id=contract.tenant_id,
            contract_id=contract.id,
            sla_deadline=moment + deadline_in,
            status=status,
            resolved_at=moment if status is EscalationStatus.RESOLVED else None,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def add_notification(
    session_factory: sessionmaker[Session],
    contract: Contract,
    *,
    channel: Channel = Channel.EMAIL,
    status: NotificationStatus = NotificationStatus.SENT,
    sent_at: object = ...,
    template: str = "T-30",
) -> int:
    """One notification row in a chosen state, as a channel-mix input.

    ``sent_at`` defaults to "a moment ago" — inside the window — and accepts an
    explicit instant or ``None``. ``template`` is varied by each caller because the
    model's ``uq_notification_slot`` is unique on
    ``(contract_id, template, scheduled_date)``: two rows wanting the same slot have
    to differ somewhere, and the slot is not the thing under test here.
    """
    when: Any = utc_now() if sent_at is ... else sent_at
    with session_scope(session_factory) as session:
        row = Notification(
            tenant_id=contract.tenant_id,
            contract_id=contract.id,
            channel=channel,
            template=template,
            status=status,
            scheduled_date=utc_now().date(),
            sent_at=when,
            idempotency_key=f"dash-{uuid.uuid4().hex}",
        )
        session.add(row)
        session.flush()
        return int(row.id)


def auth(
    client: TestClient,
    owner: Tenant,
    make_user: Callable[..., User],
    *,
    role: UserRole = UserRole.ADMIN,
    email: str = EMAIL,
) -> dict[str, str]:
    """Create one user in ``owner`` with ``role`` and return their bearer headers."""
    make_user(owner, email=email, role=role)
    response = client.post(
        LOGIN_PATH,
        json={"tenant": owner.slug, "email": email, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def read(client: TestClient, headers: dict[str, str], **params: str) -> dict[str, Any]:
    response = client.get(DASHBOARD_PATH, headers=headers, params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def audit_rows(session_factory: sessionmaker[Session]) -> int:
    with session_scope(session_factory) as session:
        return int(session.execute(select(func.count()).select_from(AuditLog)).scalar_one())


# --- the counts -----------------------------------------------------------
def test_every_lifecycle_state_is_present_and_unused_ones_read_zero(
    client: TestClient,
    tenant: Tenant,
    make_user: Callable[..., User],
    make_contract: Callable[..., Contract],
) -> None:
    """A board draws a row per state, so a state nothing is in must say ``0``.

    Two failure modes, both silent: a *missing* key leaves the client to guess (and
    it guesses whatever the last screen showed), and a *missing zero* makes the
    status rows stop summing to the headline. Asserting the full key set is the
    only assertion that catches the first.
    """
    make_contract(tenant, status=ContractStatus.ACTIVE)
    make_contract(tenant, status=ContractStatus.RENEWED)
    headers = auth(client, tenant, make_user)

    body = read(client, headers)

    by_status = body["contracts_by_status"]
    assert set(by_status) == {status.value for status in ContractStatus}
    assert by_status["active"] == 1
    assert by_status["renewed"] == 1
    assert by_status["expired"] == 0
    assert sum(by_status.values()) == body["contracts_total"] == 2


def test_the_total_is_every_contract_not_the_handful_the_panel_shows(
    client: TestClient,
    tenant: Tenant,
    make_user: Callable[..., User],
    make_contract: Callable[..., Contract],
) -> None:
    """The recent-activity panel is a page; the headline is not.

    One more contract than the panel holds, so ``contracts_total`` and
    ``len(recent_contracts)`` differ. A route that had returned the page length —
    the mistake every other read in this API makes — would pass a test that only
    checked the panel was non-empty.
    """
    for index in range(RECENT_CONTRACT_LIMIT + 1):
        make_contract(tenant, customer_ref=f"Customer {index}")
    headers = auth(client, tenant, make_user)

    body = read(client, headers)

    assert body["contracts_total"] == RECENT_CONTRACT_LIMIT + 1
    assert len(body["recent_contracts"]) == RECENT_CONTRACT_LIMIT
    # Newest first, so the panel is the tail of the insertion order, not its head.
    refs = [item["customer_ref"] for item in body["recent_contracts"]]
    assert refs[0] == f"Customer {RECENT_CONTRACT_LIMIT}"
    assert refs[-1] == "Customer 1"


def test_expiring_counts_live_terms_inside_the_window_only(
    client: TestClient,
    tenant: Tenant,
    make_user: Callable[..., User],
    make_contract: Callable[..., Contract],
) -> None:
    """Four contracts, one of which should be counted.

    The excluded three are excluded for three *different* reasons — outside the
    window, already addressed by a person, and no term at all — because a single
    negative would pass against a query that happened to drop the right rows for
    the wrong reason.
    """
    today = utc_now().date()
    make_contract(tenant, end_date=today + timedelta(days=10))
    make_contract(
        tenant,
        status=ContractStatus.RESPONDED,
        end_date=today + timedelta(days=10),
    )
    make_contract(tenant, end_date=today + timedelta(days=DEFAULT_EXPIRING_WINDOW_DAYS + 1))
    make_contract(tenant, end_date=None)
    headers = auth(client, tenant, make_user)

    body = read(client, headers)

    assert body["contracts_expiring"] == 1
    # The window is stated, not implied, so the number is checkable.
    assert body["today"] == today.isoformat()
    assert body["window_end"] == (today + timedelta(days=DEFAULT_EXPIRING_WINDOW_DAYS)).isoformat()


def test_the_window_includes_the_terms_ending_on_its_own_boundaries(
    client: TestClient,
    tenant: Tenant,
    make_user: Callable[..., User],
    make_contract: Callable[..., Contract],
) -> None:
    """Both ends are inclusive, and the day after ``window_end`` is out.

    An off-by-one here is invisible in production: a contract ending exactly
    thirty days out would simply never be mentioned, and nobody would notice a
    missing row on a number they cannot enumerate.
    """
    today = utc_now().date()
    make_contract(tenant, end_date=today)
    make_contract(tenant, end_date=today + timedelta(days=DEFAULT_EXPIRING_WINDOW_DAYS))
    make_contract(tenant, end_date=today + timedelta(days=DEFAULT_EXPIRING_WINDOW_DAYS + 1))
    headers = auth(client, tenant, make_user)

    assert read(client, headers)["contracts_expiring"] == 2


def test_review_backlog_and_escalation_queue_are_counted_not_paged(
    client: TestClient,
    tenant: Tenant,
    make_user: Callable[..., User],
    make_contract: Callable[..., Contract],
    session_factory: sessionmaker[Session],
) -> None:
    """Counts over rows the queue routes would have shown one page at a time.

    Also pins that ``escalations_overdue`` is a **subset** of
    ``escalations_open``: three open rows, one of them late. A route that added the
    two numbers would report four and be believed.
    """
    contract = make_contract(tenant)
    add_field(session_factory, contract, field_name=FieldName.EMAIL)
    add_field(session_factory, contract, field_name=FieldName.PHONE)
    add_field(session_factory, contract, field_name=FieldName.ADDRESS)
    add_field(session_factory, contract, field_name=FieldName.DOB, status=FieldStatus.APPROVED)
    add_escalation(session_factory, contract, deadline_in=timedelta(hours=-1))
    add_escalation(
        session_factory,
        contract,
        status=EscalationStatus.IN_PROGRESS,
        deadline_in=timedelta(days=2),
    )
    add_escalation(
        session_factory,
        contract,
        status=EscalationStatus.SLA_BREACHED,
        deadline_in=timedelta(days=3),
    )
    add_escalation(
        session_factory,
        contract,
        status=EscalationStatus.RESOLVED,
        deadline_in=timedelta(days=-5),
    )
    headers = auth(client, tenant, make_user)

    body = read(client, headers)

    assert body["fields_awaiting_review"] == 3
    assert body["escalations_open"] == 3
    assert body["escalations_overdue"] == 1


def test_an_empty_portfolio_reads_as_zeros_rather_than_an_error(
    client: TestClient, tenant: Tenant, make_user: Callable[..., User]
) -> None:
    """A brand-new organisation's first visit must not look broken."""
    headers = auth(client, tenant, make_user)

    body = read(client, headers)

    assert body["contracts_total"] == 0
    assert set(body["contracts_by_status"].values()) == {0}
    assert body["contracts_expiring"] == 0
    assert body["fields_awaiting_review"] == 0
    assert body["escalations_open"] == 0
    assert body["escalations_overdue"] == 0
    assert body["recent_contracts"] == []
    # The ladder is present and empty rather than absent: a client that had to guess
    # the milestones would draw a panel from whatever it last saw.
    assert [window["milestone"] for window in body["reminder_windows"]] == [
        milestone for milestone, _days in DEFAULT_MILESTONE_DAYS
    ]
    assert {window["contracts"] for window in body["reminder_windows"]} == {0}


# --- reminder windows -----------------------------------------------------
def test_the_bands_are_the_planners_ladder_and_they_abut() -> None:
    """Pure arithmetic, no database: the bands must tile the window exactly.

    Written against the function rather than the route because this is the part a
    route test cannot see — a gap between two bands and an overlap between two
    bands produce the *same* total when the sum is asserted alone. Each boundary is
    therefore checked by name: the bands are listed loosest first, so each entry
    ends the day before the entry above it begins, and the tightest band starts on
    ``today`` itself.
    """
    today = date(2026, 3, 1)

    bands = reminder_window_bands(today=today)

    assert [(milestone, days) for milestone, days, _start, _end in bands] == list(
        DEFAULT_MILESTONE_DAYS
    )
    for index, (_milestone, days, _start, end) in enumerate(bands):
        assert end == today + timedelta(days=days)
        if index == 0:
            continue
        # Reading down the list walks backwards through the window: the looser band
        # above begins the day after this one ends.
        looser_start = bands[index - 1][2]
        assert looser_start == end + timedelta(days=1)
    assert bands[-1][2] == today
    assert bands[0][3] == today + timedelta(
        days=max(days for _milestone, days in DEFAULT_MILESTONE_DAYS)
    )


def test_each_term_lands_in_exactly_one_band(
    client: TestClient,
    tenant: Tenant,
    make_user: Callable[..., User],
    make_contract: Callable[..., Contract],
) -> None:
    """One contract per boundary, and the sum is the whole ladder's count.

    The interesting rows are the ones *on* a boundary: a term ending exactly thirty
    days out is due a ``T-30`` reminder today, not a ``T-60`` one, because
    ``milestone_for`` takes the tightest milestone already due. A naive band of
    ``<= 60`` for T-60 and ``<= 30`` for T-30 would count that row twice and still
    look plausible on screen.
    """
    today = utc_now().date()
    for days in (61, 60, 31, 30, 15, 14, 1, 0, -1):
        make_contract(tenant, end_date=today + timedelta(days=days))
    make_contract(tenant, end_date=None)
    headers = auth(client, tenant, make_user)

    body = read(client, headers)

    counts = {window["milestone"]: window["contracts"] for window in body["reminder_windows"]}
    # 60 and 31 in T-60 (61 is beyond the ladder), 30 and 15 in T-30, 0/1/14 in T-14.
    assert counts == {"T-60": 2, "T-30": 2, "T-14": 3}
    # Seven terms sit inside the ladder. ``contracts_expiring`` is a *tighter* window
    # (30 days, not 60), so it holds five of them: the rows at day 31 and day 60 are in
    # T-60 and no band the expiring headline reads. The two figures are stated rather
    # than derived from each other, because they answer different questions and an
    # offset between them is arithmetic nobody would notice going wrong.
    assert sum(counts.values()) == 7
    assert body["contracts_expiring"] == 5
    ladder_end = today + timedelta(days=max(days for _milestone, days in DEFAULT_MILESTONE_DAYS))
    assert body["reminder_windows"][0]["start"] == (today + timedelta(days=31)).isoformat()
    assert body["reminder_windows"][0]["end"] == ladder_end.isoformat()


def test_the_bands_hold_the_states_the_planner_would_still_remind(
    client: TestClient,
    tenant: Tenant,
    make_user: Callable[..., User],
    make_contract: Callable[..., Contract],
) -> None:
    """Same predicate as ``contracts_expiring``, asserted on a band rather than a total.

    Three rows end inside the final fortnight and only one is still owed a reminder:
    a customer who responded, a contract somebody renewed, and one already escalated.
    A band counting statuses the planner would refuse to send for would put work on
    the screen that the dispatch worker will decline.
    """
    today = utc_now().date()
    make_contract(tenant, end_date=today + timedelta(days=7))
    make_contract(tenant, status=ContractStatus.RESPONDED, end_date=today + timedelta(days=7))
    make_contract(tenant, status=ContractStatus.RENEWED, end_date=today + timedelta(days=7))
    make_contract(tenant, status=ContractStatus.ESCALATED, end_date=today + timedelta(days=7))
    headers = auth(client, tenant, make_user)

    body = read(client, headers)

    counts = {window["milestone"]: window["contracts"] for window in body["reminder_windows"]}
    # All four terms end inside the final fortnight, but a band is the *planner's*
    # view: it counts only the states ``count_expiring`` counts, so the answered,
    # renewed and escalated rows are in no band at all rather than in T-14 with the
    # one contract still owed a reminder.
    assert counts == {"T-60": 0, "T-30": 0, "T-14": 1}


def test_a_neighbours_terms_are_in_nobodys_window(
    client: TestClient,
    tenant: Tenant,
    other_tenant: Tenant,
    make_user: Callable[..., User],
    make_contract: Callable[..., Contract],
) -> None:
    """The bands are tenant-scoped like every other count on the route.

    Written with the busy neighbour first and the quiet tenant second: a band read
    that forgot the tenant predicate would show three windows here and look like a
    working panel.
    """
    today = utc_now().date()
    make_contract(other_tenant, end_date=today + timedelta(days=5))
    busy = auth(client, other_tenant, make_user, email="ops@globex.test")
    quiet = auth(client, tenant, make_user, email=EMAIL)

    assert sum(w["contracts"] for w in read(client, busy)["reminder_windows"]) == 1
    assert sum(w["contracts"] for w in read(client, quiet)["reminder_windows"]) == 0


# --- channel mix ----------------------------------------------------------
def test_the_mix_counts_contact_that_happened_and_nothing_else(
    client: TestClient,
    tenant: Tenant,
    make_user: Callable[..., User],
    make_contract: Callable[..., Contract],
    session_factory: sessionmaker[Session],
) -> None:
    """Seven rows, three of them contact — each exclusion for its own reason.

    The three counted rows are ``sent``, ``delivered`` and ``machine``, because
    "reached the customer" is wider than "was delivered": a voicemail reached a
    mailbox even though the spec refuses to call it a response, and excluding it
    would make the board undercount the very calls that generate escalations.

    The four excluded rows fail four *different* ways — ``failed`` and
    ``suppressed`` reached nobody, ``pending`` never went out at all (no
    ``sent_at``), and one sent row is outside the window. A single negative would
    pass against a query that dropped rows for the wrong reason; only having all
    four means a wrong predicate cannot produce the right total by luck.
    """
    contract = make_contract(tenant)
    long_ago = utc_now() - timedelta(days=DEFAULT_CHANNEL_MIX_WINDOW_DAYS + 1)
    add_notification(session_factory, contract, channel=Channel.EMAIL, template="T-60")
    add_notification(
        session_factory,
        contract,
        channel=Channel.EMAIL,
        status=NotificationStatus.DELIVERED,
        template="T-30",
    )
    add_notification(
        session_factory,
        contract,
        channel=Channel.VOICE,
        status=NotificationStatus.MACHINE,
        template="T-14",
    )
    # The four that must not be counted.
    add_notification(
        session_factory,
        contract,
        status=NotificationStatus.FAILED,
        template="T-7",
    )
    add_notification(
        session_factory,
        contract,
        status=NotificationStatus.SUPPRESSED,
        template="T-3",
    )
    add_notification(
        session_factory,
        contract,
        status=NotificationStatus.PENDING,
        sent_at=None,
        template="T-1",
    )
    add_notification(session_factory, contract, sent_at=long_ago, template="T-0")
    headers = auth(client, tenant, make_user)

    body = read(client, headers)

    mix = {entry["channel"]: entry["contacts"] for entry in body["channel_mix"]}
    assert mix == {"email": 2, "sms": 0, "voice": 1}


def test_a_channel_nobody_used_reads_zero_rather_than_missing(
    client: TestClient,
    tenant: Tenant,
    make_user: Callable[..., User],
) -> None:
    """Every channel is present, in enum order, even on a silent portfolio.

    The same two failure modes the status rows have, and the same consequence: a
    missing key leaves the client to draw whatever it drew last time, and a missing
    zero makes "we never text anyone" indistinguishable from "this board forgot to
    count texts". The order is asserted too, because a panel that reorders itself
    between reads is a change nobody notices until two screenshots are compared.
    """
    headers = auth(client, tenant, make_user)

    mix = read(client, headers)["channel_mix"]

    assert [entry["channel"] for entry in mix] == [channel.value for channel in ALL_CHANNELS]
    assert [entry["contacts"] for entry in mix] == [0] * len(ALL_CHANNELS)


def test_the_mix_states_its_window_and_the_window_trails_the_reading(
    client: TestClient,
    tenant: Tenant,
    make_user: Callable[..., User],
    make_contract: Callable[..., Contract],
) -> None:
    """A count with no window beside it is not checkable, so the window ships.

    This is the one panel whose window points **backwards**: the reminder bands and
    ``contracts_expiring`` look forward at terms about to lapse, this looks back at
    sends that already happened. Both are thirty days and they are deliberately two
    constants, so a future change to the reminder horizon cannot silently rewrite
    what "the last month of contact" means.

    The bounds are calendar days rather than instants, and that is load-bearing
    rather than cosmetic: this route promises the same payload twice, and a window
    taken from ``now`` moves between two calls a millisecond apart. Checking that
    with days is also the only version a test *can* write — comparing `until` to a
    ``utc_now()`` captured here would fail whenever that comparison straddled
    midnight, so it is pinned to the ``today`` the payload itself reports instead.
    """
    make_contract(tenant)
    headers = auth(client, tenant, make_user)
    before = utc_now().date()

    body = read(client, headers)

    since = date.fromisoformat(body["channel_mix_since"])
    until = date.fromisoformat(body["channel_mix_until"])
    assert until - since == timedelta(days=DEFAULT_CHANNEL_MIX_WINDOW_DAYS)
    assert since < until
    # It trails the reading rather than leading it: the last counted day is the day
    # the reading was taken, so the whole window is already in the past — that is
    # what makes this a history and not a horizon.
    assert until == date.fromisoformat(body["today"])
    assert since < before


def test_the_window_ends_at_midnight_after_its_last_day(
    client: TestClient,
    tenant: Tenant,
    make_user: Callable[..., User],
    make_contract: Callable[..., Contract],
    session_factory: sessionmaker[Session],
) -> None:
    """Both ends are *days*, so the far edge is midnight and not a rounded instant.

    The window is stored as two dates and turned into a half-open instant range on
    the way to SQL. Getting that conversion wrong in either direction is invisible
    in every other test in this file: an ``until`` compared with ``<`` against
    ``time.max`` would silently drop a send in the last microsecond of the day, and
    a ``since`` compared with ``<`` would silently drop a send at exactly midnight
    when the window opened. Both edges are pinned here, one row each.
    """
    contract = make_contract(tenant)
    last_day = utc_now().date()
    add_notification(
        session_factory,
        contract,
        sent_at=datetime.combine(last_day, time.max),
        template="T-60",
    )
    add_notification(
        session_factory,
        contract,
        channel=Channel.SMS,
        sent_at=datetime.combine(last_day + timedelta(days=1), time(0, 0)),
        template="T-30",
    )
    add_notification(
        session_factory,
        contract,
        channel=Channel.VOICE,
        sent_at=datetime.combine(
            last_day - timedelta(days=DEFAULT_CHANNEL_MIX_WINDOW_DAYS), time(0, 0)
        ),
        template="T-14",
    )
    headers = auth(client, tenant, make_user)

    mix = {entry["channel"]: entry["contacts"] for entry in read(client, headers)["channel_mix"]}

    # The send at 00:00 on the window's first day is inside it; the one at 00:00 the
    # morning after its last day is not.
    assert mix == {"email": 1, "sms": 0, "voice": 1}


def test_a_neighbours_contacts_are_in_nobodys_mix(
    client: TestClient,
    tenant: Tenant,
    other_tenant: Tenant,
    make_user: Callable[..., User],
    make_contract: Callable[..., Contract],
    session_factory: sessionmaker[Session],
) -> None:
    """Three sends at the neighbour, one here: neither board may see the other.

    The quiet side carries the assertion. A grouped ``COUNT`` that dropped the
    tenant predicate is the easiest leak in this file to write — ``group_by`` reads
    like it already scoped the rows — and the busy tenant would look perfectly
    correct while the leak happened.
    """
    add_notification(session_factory, make_contract(other_tenant), template="T-60")
    add_notification(
        session_factory, make_contract(other_tenant), channel=Channel.SMS, template="T-30"
    )
    add_notification(
        session_factory, make_contract(other_tenant), channel=Channel.VOICE, template="T-14"
    )
    add_notification(session_factory, make_contract(tenant), template="T-60")

    busy = auth(client, other_tenant, make_user, email="ops@globex.test")
    quiet = auth(client, tenant, make_user, email=EMAIL)

    busy_mix = {e["channel"]: e["contacts"] for e in read(client, busy)["channel_mix"]}
    quiet_mix = {e["channel"]: e["contacts"] for e in read(client, quiet)["channel_mix"]}
    assert busy_mix == {"email": 1, "sms": 1, "voice": 1}
    assert quiet_mix == {"email": 1, "sms": 0, "voice": 0}


# --- isolation ------------------------------------------------------------
def test_one_organisations_totals_reach_nobody_else(
    client: TestClient,
    tenant: Tenant,
    other_tenant: Tenant,
    make_user: Callable[..., User],
    make_contract: Callable[..., Contract],
    session_factory: sessionmaker[Session],
) -> None:
    """The busy neighbour is unchanged; the clean one reads zeros.

    The clean side is the assertion that matters. A route that counted every row in
    the table would show the neighbour's three contracts to an organisation with
    none, and a test that only checked the busy tenant would never notice.
    """
    contract = make_contract(tenant, end_date=utc_now().date() + timedelta(days=5))
    add_field(session_factory, contract)
    add_escalation(session_factory, contract)

    busy = auth(client, tenant, make_user, email=EMAIL)
    quiet = auth(client, other_tenant, make_user, email="ops@globex.test")

    busy_body = read(client, busy)
    quiet_body = read(client, quiet)

    assert busy_body["contracts_total"] == 1
    assert busy_body["fields_awaiting_review"] == 1
    assert busy_body["escalations_open"] == 1

    assert quiet_body["contracts_total"] == 0
    assert quiet_body["fields_awaiting_review"] == 0
    assert quiet_body["escalations_open"] == 0
    assert quiet_body["escalations_overdue"] == 0
    assert quiet_body["recent_contracts"] == []


def test_the_tenant_cannot_be_asked_for(
    client: TestClient,
    tenant: Tenant,
    other_tenant: Tenant,
    make_user: Callable[..., User],
    make_contract: Callable[..., Contract],
) -> None:
    """A caller naming somebody else's tenant gets their own board back.

    The parameter is not rejected with a 422, because it is not declared — FastAPI
    ignores unknown query parameters. What matters is that naming a tenant changes
    nothing: ``principal.tenant_id`` comes from the token and a database read.
    """
    make_contract(other_tenant, customer_ref="Someone Else")
    headers = auth(client, tenant, make_user)

    body = read(client, headers, tenant_id=str(other_tenant.id))

    assert body["contracts_total"] == 0
    assert body["recent_contracts"] == []


# --- access and side effects ---------------------------------------------
def test_a_viewer_may_read_the_portfolio(
    client: TestClient,
    tenant: Tenant,
    make_user: Callable[..., User],
    make_contract: Callable[..., Contract],
) -> None:
    """Counts of rows a viewer can already open are not an admin secret.

    The attention board is admin-only because its payload is a list of field
    primary keys; this one gives away nothing a viewer cannot reach from the
    contracts screen they already have.
    """
    make_contract(tenant)
    headers = auth(client, tenant, make_user, role=UserRole.VIEWER)

    assert read(client, headers)["contracts_total"] == 1


def test_reading_it_writes_nothing(
    client: TestClient,
    tenant: Tenant,
    make_user: Callable[..., User],
    make_contract: Callable[..., Contract],
    session_factory: sessionmaker[Session],
) -> None:
    """Two reads, same answer, no audit row and no version bump.

    ``version`` is the optimistic-lock counter on every contract, so a route that
    touched a row while counting it would be visible here even though it wrote no
    column anybody displays.
    """
    make_contract(tenant)
    headers = auth(client, tenant, make_user)
    before_audit = audit_rows(session_factory)
    with session_scope(session_factory) as session:
        before_version = session.execute(select(Contract.version)).scalar_one()

    first = read(client, headers)
    second = read(client, headers)

    assert first == second
    assert audit_rows(session_factory) == before_audit
    with session_scope(session_factory) as session:
        assert session.execute(select(Contract.version)).scalar_one() == before_version


def test_the_route_needs_a_token(client: TestClient) -> None:
    """No token, no board — the tenant has to come from somewhere.

    Pinned because "it returned zeros" and "it let me in" look identical from the
    outside, and only one of them is acceptable.
    """
    assert client.get(DASHBOARD_PATH).status_code == 401


def test_the_window_boundaries_are_dates_not_timestamps(
    client: TestClient,
    tenant: Tenant,
    make_user: Callable[..., User],
    make_contract: Callable[..., Contract],
) -> None:
    """``today``/``window_end`` serialize as ``YYYY-MM-DD``.

    A ``datetime`` here would carry an offset the route never meant to promise, and
    a client comparing it to a contract's ``end_date`` would start doing timezone
    arithmetic on two values that are calendar days.
    """
    make_contract(tenant)
    headers = auth(client, tenant, make_user)

    body = read(client, headers)

    assert isinstance(body["today"], str)
    assert isinstance(body["window_end"], str)
    assert len(body["today"]) == 10
    assert len(body["window_end"]) == 10
    assert date.fromisoformat(body["today"]) <= date.fromisoformat(body["window_end"])
    assert "T" not in body["today"]
