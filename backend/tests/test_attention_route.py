"""The attention report over HTTP: one organisation's own board (gap 22).

``app.core.attention`` had answered "what needs a human?" since ``D10-39``, but only
for a reader with no principal. ``D10-39``/``D10-40`` recorded the missing screen as
*blocked* rather than *forgotten*: the three counting functions behind the operator
reading scan every tenant at once, so putting them behind a route would have
published one organisation's counts and field ids to another, and the recorded
conclusion was that resolving it was an owner decision rather than a patch.

The route resolves it by being a second *reader* rather than a narrowed one. Those
two functions are untouched and still answer the operator's question; this test file
exists to pin the new reader's one dangerous property, which is not "it returns
numbers" but:

* **A tenant's board contains only their own rows.** Two organisations, each with
  rows of both kinds, each admin seeing their own counts and their own field ids —
  and the harder direction, a *clean* organisation beside a dirty one, because an
  unscoped read that leaked nothing in the busy direction is still a leak.
* **The tenant cannot be asked for.** The route accepts no tenant parameter, and a
  request that tries anyway gets the caller's own board rather than the one it
  named. ``principal.tenant_id`` comes from the bearer token and a database read.
* **The park count is absent, on purpose.** ``outbox_parked`` is a relay fault
  attributable to the deployment, so it is counted by the operator reading and
  never put on a tenant's board. The payload is compared as an exact dictionary,
  which is the only way an *absent* key can be pinned — asserting the keys one
  likes would pass just as happily with a third one added.
* **The role that gets it is the narrowest one that can act on it.** Admin only:
  the row-level detail is a list of field primary keys, and a viewer has no screen
  on which to act on a failed notification.
* **Reading it changes nothing.** No audit row, no lock, no write — the same answer
  twice, from a route on the read-only side of the app.

The operator reading is asserted here too, once, for the reason ``D10-39`` spent a
section on: the two readers must be able to disagree about the same database without
either being wrong. One test reads both and requires different numbers, because a
tenant-scoped route that silently narrowed ``attention_report`` would pass every
other test in this file.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.attention import attention_report
from app.core.config import Settings
from app.core.crypto import FieldCipher
from app.db.base import utc_now
from app.db.enums import (
    Channel,
    ContractStatus,
    FieldName,
    FieldStatus,
    NotificationStatus,
    OutboxStatus,
    UserRole,
)
from app.db.models import (
    AuditLog,
    Contract,
    ExtractedField,
    Notification,
    OutboxEvent,
    Tenant,
    User,
)
from app.db.session import session_scope
from app.main import create_app
from app.workers.dispatch import notification_idempotency_key

ATTENTION_PATH = "/api/v1/attention"
LOGIN_PATH = "/api/v1/auth/login"

#: Matches ``conftest.DEFAULT_PASSWORD``; protects in-memory databases only.
PASSWORD = "correct-horse-battery-staple"  # nosec B105
EMAIL = "ops@acme.test"
OTHER_EMAIL = "ops@globex.test"
PLAINTEXT_EMAIL = "a.rahman@example.test"
PLAINTEXT_PHONE = "+442079460958"

KEY = Fernet.generate_key().decode("ascii")
CIPHER = FieldCipher(KEY)


# --- fixtures -------------------------------------------------------------
@pytest.fixture
def tiny_scan_client(
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> Iterator[TestClient]:
    """A client whose app scans one field row per read.

    ``Settings`` is copied rather than mutated, as ``test_review`` does for the
    encryption key: the plain ``client`` fixture has to keep its shipped limit, and
    the truncation case needs a limit small enough to fill in one test.
    """
    app = create_app(
        settings.model_copy(update={"attention_field_scan_limit": 1}),
        session_factory=session_factory,
    )
    with TestClient(app) as test_client:
        yield test_client


# --- helpers --------------------------------------------------------------
def make_contract(session_factory: sessionmaker[Session], owner: Tenant) -> Contract:
    """One contract for ``owner``, the row every notification and field hangs off."""
    with session_scope(session_factory) as session:
        row = Contract(
            tenant_id=owner.id,
            customer_ref="A. Rahman",
            timezone="UTC",
            status=ContractStatus.ACTIVE,
        )
        session.add(row)
        session.flush()
        return row


def add_notification(
    session_factory: sessionmaker[Session],
    contract: Contract,
    status: NotificationStatus,
    *,
    template: str = "T-60",
    slot: date = date(2025, 1, 2),
) -> int:
    """Insert one notification row in a chosen state, bypassing the dispatcher.

    Reaching ``failed`` or ``suppressed`` through the real worker needs a refusing
    transport and a spent retry budget; the state is the subject here, not the route
    that produced it — the same reason ``test_doctor`` writes the row directly.
    """
    with session_scope(session_factory) as session:
        row = Notification(
            tenant_id=contract.tenant_id,
            contract_id=contract.id,
            channel=Channel.EMAIL,
            template=template,
            status=status,
            scheduled_date=slot,
            idempotency_key=notification_idempotency_key(contract.id, template, slot),
            attempts=5 if status is NotificationStatus.FAILED else 0,
            last_error="relay refused" if status is NotificationStatus.FAILED else None,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def add_field(
    session_factory: sessionmaker[Session],
    contract: Contract,
    *,
    field_name: FieldName = FieldName.EMAIL,
    raw: str = PLAINTEXT_EMAIL,
    normalized: str | None = None,
    sealed: bool = False,
) -> int:
    """Insert one field row and return its id.

    ``normalized`` defaults to ``raw`` so the ordinary case is one value in both
    columns; a mixed row is expressible because the legacy writer left exactly that
    shape behind, and ``is_sealed`` is applied per column.
    """
    normalized_value = raw if normalized is None else normalized
    with session_scope(session_factory) as session:
        row = ExtractedField(
            tenant_id=contract.tenant_id,
            contract_id=contract.id,
            field_name=field_name,
            raw_value=CIPHER.encrypt(raw) if sealed else raw,
            normalized_value=CIPHER.encrypt(normalized_value) if sealed else normalized_value,
            confidence=0.93,
            status=FieldStatus.APPROVED,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def add_parked_event(session_factory: sessionmaker[Session], owner: Tenant) -> int:
    """One dead-lettered outbox row: the state the operator counts and tenants do not."""
    with session_scope(session_factory) as session:
        row = OutboxEvent(
            correlation_id="corr-attention-route",
            tenant_id=owner.id,
            event_type="contract.ingested",
            topic="contract.events",
            partition_key="1",
            payload={"contract_id": 1},
            status=OutboxStatus.FAILED,
            attempts=5,
            next_attempt_at=utc_now(),
            last_error="broker refused",
        )
        session.add(row)
        session.flush()
        return int(row.id)


def login(client: TestClient, owner: Tenant, email: str) -> dict[str, str]:
    response = client.post(
        LOGIN_PATH,
        json={"tenant": owner.slug, "email": email, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


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
    return login(client, owner, email)


def read(client: TestClient, headers: dict[str, str], **params: str) -> dict[str, Any]:
    response = client.get(ATTENTION_PATH, headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def audit_rows(session_factory: sessionmaker[Session]) -> list[AuditLog]:
    with session_scope(session_factory) as session:
        return list(session.execute(select(AuditLog).order_by(AuditLog.id)).scalars())


#: The exact wire shape of a clean board. Compared as a whole in several tests: an
#: added field has to be admitted here, deliberately, which is how the *absence* of
#: ``outbox_parked`` is pinned rather than assumed.
CLEAN = {
    "notifications_failed": 0,
    "notifications_suppressed": 0,
    "unsealed_field_values": 0,
    "unsealed_field_ids": [],
    "fields_scanned": 0,
    "field_scan_truncated": False,
    "needs_attention": False,
}


# --- a clean board is an answer, not an error -----------------------------
def test_a_clean_board_is_zeroes_and_no_attention(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    """A board with nothing on it is ``200`` with every count zero.

    Not a 404 and not an empty body: "nothing on my board needs me" is the answer
    the screen exists to render, and it is the answer on the first day of every
    deployment. The operator reading has the same property, which is why ``doctor``
    exits ``0`` on a clean database.
    """
    headers = auth(client, tenant, make_user)
    assert read(client, headers) == CLEAN


# --- the guarantee this file exists for -----------------------------------
def test_each_admin_sees_only_their_own_organisation(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
    other_tenant: Tenant,
) -> None:
    """Two organisations, rows of both kinds each, and no number shared.

    Acme: one ``failed``, one ``suppressed``, one plaintext field. Globex: two
    ``failed``, no ``suppressed``, one plaintext field. Every count differs between
    the two boards, so a route that ignored the tenant would return the totals
    (3/1/2) to both and fail here twice over.

    The extra ``tenant_id`` query parameter is part of the test rather than an
    afterthought: the route takes no tenant from the request, so a caller who names
    another organisation must get their own board back, not a ``400``. Refusing the
    parameter would be defensible too, but the property that matters is that the id
    never comes off the wire, and silently ignoring it proves exactly that.
    """
    acme_contract = make_contract(session_factory, tenant)
    acme_field = add_field(session_factory, acme_contract)
    add_notification(session_factory, acme_contract, NotificationStatus.FAILED)
    add_notification(
        session_factory,
        acme_contract,
        NotificationStatus.SUPPRESSED,
        template="T-30",
    )

    globex_contract = make_contract(session_factory, other_tenant)
    globex_field = add_field(
        session_factory,
        globex_contract,
        field_name=FieldName.PHONE,
        raw=PLAINTEXT_PHONE,
    )
    add_notification(session_factory, globex_contract, NotificationStatus.FAILED)
    add_notification(
        session_factory,
        globex_contract,
        NotificationStatus.FAILED,
        template="T-7",
    )

    acme = read(
        client,
        auth(client, tenant, make_user),
        tenant_id=str(other_tenant.id),
    )
    assert acme["notifications_failed"] == 1
    assert acme["notifications_suppressed"] == 1
    assert acme["unsealed_field_ids"] == [acme_field]
    assert globex_field not in acme["unsealed_field_ids"]

    globex = read(client, auth(client, other_tenant, make_user, email=OTHER_EMAIL))
    assert globex["notifications_failed"] == 2
    assert globex["notifications_suppressed"] == 0
    assert globex["unsealed_field_ids"] == [globex_field]
    assert acme_field not in globex["unsealed_field_ids"]


def test_a_clean_organisation_beside_a_dirty_one_stays_clean(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
    other_tenant: Tenant,
) -> None:
    """The leak direction that a busy-than-theirs test cannot catch.

    Acme has nothing at all; every finding in the database belongs to Globex. An
    unscoped read shows Acme a finding that is not theirs and would fail the busy
    test above as well — but a *partially* scoped read (one that filtered the
    notifications and forgot the field scan) shows up here and nowhere else. The
    whole clean payload is compared, so the field ids have to be empty and not
    merely "not the other tenant's".
    """
    dirty = make_contract(session_factory, other_tenant)
    add_field(session_factory, dirty)
    add_notification(session_factory, dirty, NotificationStatus.SUPPRESSED)
    add_notification(session_factory, dirty, NotificationStatus.FAILED, template="T-30")

    assert read(client, auth(client, tenant, make_user)) == CLEAN


# --- what the board deliberately does not say -----------------------------
def test_a_parked_event_is_on_the_operators_board_and_not_on_a_tenants(
    client: TestClient,
    session_factory: sessionmaker[Session],
    settings: Settings,
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    """A park is counted by the operator reading and absent from the wire.

    The judgement the route records: a relay fault is attributable to the
    deployment, so a tenant cannot act on it and a count on their board would
    produce a support ticket about somebody else's misconfigured transport. The
    operator reading keeps the number — asserted here in the same test, so "we
    dropped the park count from the tally" cannot pass as "we do not report it to
    tenants".

    The exact-dictionary comparison is what makes the absence a claim: a key that
    crept back into the payload lands here as a failure, where an assertion over
    selected keys would not notice.
    """
    contract = make_contract(session_factory, tenant)
    add_parked_event(session_factory, tenant)

    assert read(client, auth(client, tenant, make_user)) == CLEAN

    # The operator reading still sees the park, and still sees it as unexplained
    # work only if a transport is configured; here the shipped default counts it.
    with session_scope(session_factory) as session:
        operator = attention_report(session, limit=500, settings=settings)
    assert operator.outbox_parked == 1
    assert operator.notifications_failed == 0
    assert contract is not None  # the park belongs to a tenant that has a contract


def test_the_two_readings_disagree_about_the_same_database(
    client: TestClient,
    session_factory: sessionmaker[Session],
    settings: Settings,
    make_user: Callable[..., User],
    tenant: Tenant,
    other_tenant: Tenant,
) -> None:
    """One database, two questions, two right answers.

    ``D10-39`` and ``D10-40`` left the route "blocked" because the operator's
    numbers are, by design, every tenant's. This route resolves that by adding a
    reader instead of narrowing one, and the property that proves it is that the two
    readers return *different* numbers here: three undelivered rows across the
    deployment, one of them this tenant's.

    A change that reached the operator result by scoping ``undelivered_counts`` or
    ``fields_for_reseal`` would make these equal, and every other test in this file
    would still pass.
    """
    acme = make_contract(session_factory, tenant)
    globex = make_contract(session_factory, other_tenant)
    add_notification(session_factory, acme, NotificationStatus.FAILED)
    add_notification(session_factory, globex, NotificationStatus.FAILED)
    add_notification(session_factory, globex, NotificationStatus.FAILED, template="T-30")
    add_field(session_factory, globex, field_name=FieldName.PHONE, raw=PLAINTEXT_PHONE)

    with session_scope(session_factory) as session:
        operator = attention_report(session, limit=500, settings=settings)
    assert operator.notifications_failed == 3
    assert operator.unsealed_field_values == 1

    mine = read(client, auth(client, tenant, make_user))
    assert mine["notifications_failed"] == 1
    assert mine["unsealed_field_values"] == 0


# --- what the field scan does and does not count --------------------------
def test_a_sealed_row_is_scanned_and_not_counted(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    """``is_sealed`` is shape-based, so a sealed row is read and not reported.

    ``fields_scanned`` moving to ``1`` while the count stays ``0`` is the assertion
    that separates "the query found nothing" from "the query found a sealed row and
    did not call it a finding". Without both numbers a broken scan and a clean board
    look identical — which is the failure mode the operator check exists to remove.
    """
    contract = make_contract(session_factory, tenant)
    field_id = add_field(session_factory, contract, sealed=True)

    board = read(client, auth(client, tenant, make_user))
    assert board["unsealed_field_values"] == 0
    assert board["unsealed_field_ids"] == []
    assert board["fields_scanned"] == 1
    assert board["field_scan_truncated"] is False
    assert board["needs_attention"] is False
    assert field_id > 0


def test_one_unsealed_column_is_enough_to_count_a_row(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    """The pre-gap-20 shape: ``raw_value`` sealed while ``normalized_value`` was not.

    This is exactly what the old writer left behind, and it is the row a naive "is
    the row encrypted?" check misses. Counting rows rather than columns is also
    pinned: such a row contributes one value here, not two.
    """
    contract = make_contract(session_factory, tenant)
    mixed = add_field(
        session_factory,
        contract,
        sealed=True,
    )
    # Seal-written row above counts zero; now the genuinely mixed row.
    mixed_id = add_field(
        session_factory,
        contract,
        field_name=FieldName.PHONE,
        raw=PLAINTEXT_PHONE,
        normalized=CIPHER.encrypt(PLAINTEXT_PHONE),
    )

    board = read(client, auth(client, tenant, make_user))
    assert board["unsealed_field_values"] == 1
    assert board["unsealed_field_ids"] == [mixed_id]
    assert mixed not in board["unsealed_field_ids"]
    assert board["fields_scanned"] == 2
    assert board["needs_attention"] is True


def test_a_full_scan_page_is_reported_as_a_floor(
    tiny_scan_client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    """A truncated scan says so, and its count is a floor rather than a total.

    With ``ATTENTION_FIELD_SCAN_LIMIT=1`` and three plaintext rows, the route sees
    one row and reports ``field_scan_truncated``. "0 unsealed" from a truncated scan
    is the one answer this reading must never give confidently, so the flag travels
    with the count on the wire and not only in a log line.
    """
    contract = make_contract(session_factory, tenant)
    first = add_field(session_factory, contract)
    add_field(session_factory, contract, field_name=FieldName.PHONE, raw=PLAINTEXT_PHONE)
    add_field(session_factory, contract, field_name=FieldName.ADDRESS, raw="Dhaka 1212")

    board = read(tiny_scan_client, auth(tiny_scan_client, tenant, make_user))
    assert board["fields_scanned"] == 1
    assert board["field_scan_truncated"] is True
    assert board["unsealed_field_values"] == 1
    assert board["unsealed_field_ids"] == [first]


# --- who may read it ------------------------------------------------------
@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER])
def test_only_an_admin_may_read_the_board(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
    role: UserRole,
) -> None:
    """Operators and viewers are refused with the shared ``403`` envelope.

    Not because the numbers are secret — an operator may see far more of this
    organisation's data — but because the row-level detail is a list of field
    primary keys and the only actions that clear a finding live on admin screens
    they do not have. The refusal uses the same ``forbidden`` code every other route
    uses, so the frontend's shared error envelope keeps working unchanged.
    """
    headers = auth(client, tenant, make_user, role=role)
    response = client.get(ATTENTION_PATH, headers=headers)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "forbidden"


def test_an_unauthenticated_caller_is_told_to_sign_in(
    client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """No token is ``401 unauthorized``, distinct from the ``403`` above.

    The distinction matters to the screens: ``401`` sends the client to the refresh
    path and then to sign-in, while ``403`` is a role problem that signing in again
    cannot fix. Collapsing the two would either loop a viewer through sign-in or
    strand an expired session on a dead screen.
    """
    response = client.get(ATTENTION_PATH)
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "unauthorized"
    assert tenant is not None


# --- reading changes nothing ----------------------------------------------
def test_reading_the_board_writes_nothing_and_answers_twice(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    """No audit row, no write, and the same answer on a second read.

    The audit log is the assertion that matters: every other read in this app that
    touches regulated data leaves a row, and this one deliberately does not, because
    it reads no value at all — only ids and counts. A route that started writing
    audit rows would be indistinguishable from a correct one in the payload, and
    would make the screen's polling expensive in a way nobody would notice.
    """
    contract = make_contract(session_factory, tenant)
    add_field(session_factory, contract)
    headers = auth(client, tenant, make_user)

    first = read(client, headers)
    second = read(client, headers)
    assert first == second
    assert first["needs_attention"] is True
    assert audit_rows(session_factory) == []
