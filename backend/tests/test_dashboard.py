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

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.v1.dashboard import (
    DEFAULT_EXPIRING_WINDOW_DAYS,
    RECENT_CONTRACT_LIMIT,
    reminder_window_bands,
)
from app.db.base import utc_now
from app.db.enums import ContractStatus, EscalationStatus, FieldName, FieldStatus, UserRole
from app.db.models import AuditLog, Contract, Escalation, ExtractedField, Tenant, User
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
