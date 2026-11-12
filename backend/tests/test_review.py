"""Human review of extracted fields (F8).

Three routes, and the tests are grouped around the guarantee each one carries:

* the queue is where extraction output becomes *findable* — tenant-scoped, worst
  confidence first, and still value-free;
* the decision is the only write that can make a field ``approved``, which is the
  only state from which a notification may eventually reach a customer — so it is
  role-gated, version-checked, and audited;
* the reveal is the one call that returns a value, and therefore the one call that
  must leave a trace.

The recurring assertion in this file is a negative one: no route, and no audit
row, ever contains the stored value.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.blind_index import ContactBlindIndex
from app.core.config import Settings
from app.core.crypto import FieldCipher
from app.db.base import utc_now
from app.db.enums import Channel, FieldName, FieldStatus, UserRole
from app.db.models import AuditLog, Contract, ExtractedField, Tenant, User
from app.db.session import session_scope
from app.main import create_app
from app.workers.dispatch import (
    REFUSAL_MISSING_END_DATE,
    DispatchRefusedError,
    build_notification,
)

LOGIN_PATH = "/api/v1/auth/login"
QUEUE_PATH = "/api/v1/review/queue"
FIELD_PATH = "/api/v1/review/{field_id}"
REVEAL_PATH = "/api/v1/review/{field_id}/reveal"

#: Matches ``conftest.DEFAULT_PASSWORD``; protects in-memory databases only.
PASSWORD = "correct-horse-battery-staple"  # nosec B105
EMAIL = "ops@acme.test"
PLAINTEXT = "a.rahman@example.test"

#: Key material for the contact blind index in tests that correct a contact value.
#: Not derived from the encryption key, because the two have different lifetimes.
BLIND_INDEX_KEY = "review-test-contact-index-key-01"

#: The shipped review threshold (``EXTRACTION_REVIEW_THRESHOLD``), named here so a
#: change to the default is visible rather than silently baked into an assertion.
THRESHOLD = 0.8


# --- fixtures -------------------------------------------------------------
@pytest.fixture
def encryption_key() -> str:
    return Fernet.generate_key().decode("ascii")


@pytest.fixture
def cipher(encryption_key: str) -> FieldCipher:
    return FieldCipher(encryption_key)


@pytest.fixture
def secured_client(
    settings: Settings,
    encryption_key: str,
    session_factory: sessionmaker[Session],
) -> Iterator[TestClient]:
    """A client whose app has both keys configured.

    ``Settings`` is copied rather than mutated: the plain ``client`` fixture (no
    key) is what proves the 503 path below, so the two configurations have to
    coexist in one test module.

    ``CONTACT_BLIND_INDEX_KEY`` is here because correcting an email or a phone
    number recomputes its digest — a correction that left a stale digest would
    leave the customer's STOP suppressing the value they no longer have.
    """
    app = create_app(
        settings.model_copy(
            update={
                "encryption_key": encryption_key,
                "contact_blind_index_key": BLIND_INDEX_KEY,
            }
        ),
        session_factory=session_factory,
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def sealed_client(
    settings: Settings,
    encryption_key: str,
    session_factory: sessionmaker[Session],
) -> Iterator[TestClient]:
    """A client that can seal values but has no inbound contact lookup.

    This is a legal deployment shape, not a broken one: with no inbound SMS there
    is nothing to look a digest up by. Such a deployment must still be able to
    review a date, which is what ``sealed_client`` exists to prove.
    """
    app = create_app(
        settings.model_copy(update={"encryption_key": encryption_key}),
        session_factory=session_factory,
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def contract(session_factory: sessionmaker[Session], tenant: Tenant) -> Contract:
    with session_scope(session_factory) as session:
        row = Contract(tenant_id=tenant.id, customer_ref="A. Rahman", end_date=date(2026, 1, 1))
        session.add(row)
        session.flush()
        return row


@pytest.fixture
def undated_contract(session_factory: sessionmaker[Session], tenant: Tenant) -> Contract:
    """A contract shaped the way ``POST /contracts/document`` leaves one: no end date.

    The fixture above ships a date, because most of this file only needs a contract
    to hang a field on. Promotion is about the rows that have *none*: the upload
    route takes ``end_date`` as an ingestion parameter, so a caller who omits it has
    no second chance to supply one, and the document's own date then exists only as
    an extracted field.

    Email consent is on record so the end-to-end test at the bottom of this file can
    put the same question to the reminder planner that the scheduler puts to it.
    """
    with session_scope(session_factory) as session:
        row = Contract(
            tenant_id=tenant.id,
            customer_ref="A. Rahman",
            end_date=None,
            consent_email=True,
        )
        session.add(row)
        session.flush()
        return row


# --- helpers --------------------------------------------------------------
def add_field(
    session_factory: sessionmaker[Session],
    contract: Contract,
    cipher: FieldCipher,
    *,
    field_name: FieldName = FieldName.EMAIL,
    value: str | None = PLAINTEXT,
    confidence: float = 0.4,
    status: FieldStatus = FieldStatus.PENDING_REVIEW,
    created_at: datetime | None = None,
) -> int:
    """Store one extracted field and return its id.

    Committed rather than flushed: the request under test runs in its own session
    and must see the row.

    ``created_at`` is settable so a test can pin the *second* term of the review
    queue's sort key and force a tie all the way down to the id.
    """
    with session_scope(session_factory) as session:
        row = ExtractedField(
            tenant_id=contract.tenant_id,
            contract_id=contract.id,
            field_name=field_name,
            raw_value=cipher.encrypt(value) if value is not None else None,
            normalized_value=cipher.encrypt(value) if value is not None else None,
            confidence=confidence,
            status=status,
        )
        if created_at is not None:
            row.created_at = created_at
        session.add(row)
        session.flush()
        return int(row.id)


def load_field(session_factory: sessionmaker[Session], field_id: int) -> ExtractedField:
    """Re-read a field through a fresh session, so assertions see stored state."""
    with session_scope(session_factory) as session:
        row = session.get(ExtractedField, field_id)
        assert row is not None
        return row


def set_raw_value(session_factory: sessionmaker[Session], field_id: int, token: str | None) -> None:
    with session_scope(session_factory) as session:
        row = session.get(ExtractedField, field_id)
        assert row is not None
        row.raw_value = token


def load_contract(session_factory: sessionmaker[Session], contract_id: int) -> Contract:
    """Re-read a contract through a fresh session.

    Assertions about the end date have to come from the database rather than the
    object a route mutated: the question is what was *stored*, and the in-request
    instance could show an assignment that was never committed.
    """
    with session_scope(session_factory) as session:
        row = session.get(Contract, contract_id)
        assert row is not None
        return row


def audit_rows(session_factory: sessionmaker[Session]) -> list[AuditLog]:
    with session_scope(session_factory) as session:
        return list(session.execute(select(AuditLog).order_by(AuditLog.id)).scalars())


def login(client: TestClient, tenant: Tenant, email: str = EMAIL) -> dict[str, str]:
    response = client.post(
        LOGIN_PATH,
        json={"tenant": tenant.slug, "email": email, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def auth(
    client: TestClient,
    tenant: Tenant,
    make_user: Callable[..., User],
    *,
    role: UserRole = UserRole.OPERATOR,
    email: str = EMAIL,
) -> dict[str, str]:
    make_user(tenant, email=email, role=role)
    return login(client, tenant, email)


# --- the queue ------------------------------------------------------------
def test_the_queue_lists_only_fields_awaiting_review(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    pending = add_field(session_factory, contract, cipher)
    add_field(
        session_factory,
        contract,
        cipher,
        field_name=FieldName.PHONE,
        value="+442079460958",
        confidence=0.99,
        status=FieldStatus.AUTO_ACCEPTED,
    )
    add_field(
        session_factory,
        contract,
        cipher,
        field_name=FieldName.START_DATE,
        value="2024-01-01",
        confidence=0.99,
        status=FieldStatus.APPROVED,
    )
    headers = auth(secured_client, tenant, make_user)

    body = secured_client.get(QUEUE_PATH, headers=headers).json()

    # Only the field a person still has to decide on: an auto-accepted or already
    # approved row is not work.
    assert body["count"] == 1
    assert [item["field_id"] for item in body["items"]] == [pending]


def test_the_queue_is_ordered_worst_confidence_first(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    low = add_field(session_factory, contract, cipher, confidence=0.2)
    high = add_field(
        session_factory,
        contract,
        cipher,
        field_name=FieldName.PHONE,
        value="+442079460958",
        confidence=0.7,
    )
    middle = add_field(
        session_factory,
        contract,
        cipher,
        field_name=FieldName.START_DATE,
        value="2024-01-01",
        confidence=0.5,
    )
    headers = auth(secured_client, tenant, make_user)

    body = secured_client.get(QUEUE_PATH, headers=headers).json()

    # The least trustworthy extraction is the one a reviewer should meet first.
    assert [item["field_id"] for item in body["items"]] == [low, middle, high]


def test_the_queue_shows_the_contract_a_field_blocks(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract, cipher)
    headers = auth(secured_client, tenant, make_user)

    item = secured_client.get(QUEUE_PATH, headers=headers).json()["items"][0]

    assert item["contract_id"] == contract.id
    assert item["customer_ref"] == "A. Rahman"
    assert item["contract_status"] == "active"
    assert item["end_date"] == "2026-01-01"
    assert isinstance(item["days_to_expiry"], int)
    assert item["age_minutes"] >= 0
    # The threshold travels with the item so a reviewer can see why it is here
    # without knowing the deployment's configuration.
    assert item["threshold"] == THRESHOLD
    assert item["status"] == "pending_review"
    assert item["blocks_pipeline"] is True


def test_the_queue_is_tenant_scoped(
    secured_client: TestClient,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    other_tenant: Tenant,
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    with session_scope(session_factory) as session:
        foreign = Contract(tenant_id=other_tenant.id, customer_ref="Globex")
        session.add(foreign)
        session.flush()
    add_field(session_factory, foreign, cipher)
    headers = auth(secured_client, tenant, make_user)

    body = secured_client.get(QUEUE_PATH, headers=headers).json()

    # Both sides of the join carry the tenant predicate, so another
    # organisation's backlog is invisible rather than merely filtered out.
    assert body == {
        "items": [],
        "count": 0,
        "oldest_minutes": 0,
        "threshold": THRESHOLD,
        "next_cursor": None,
    }


def test_the_queue_respects_its_bounds(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    for index in range(3):
        add_field(
            session_factory,
            contract,
            cipher,
            field_name=FieldName[("EMAIL", "PHONE", "ADDRESS")[index]],
            value=PLAINTEXT if index == 0 else f"+44207946095{index}",
            confidence=0.1 * (index + 1),
        )
    headers = auth(secured_client, tenant, make_user)

    body = secured_client.get(QUEUE_PATH, params={"limit": 1}, headers=headers).json()

    # Bounded at the query, so a caller cannot ask for the whole table.
    assert body["count"] == 1
    assert len(body["items"]) == 1
    for bad_limit in (0, 501):
        response = secured_client.get(QUEUE_PATH, params={"limit": bad_limit}, headers=headers)
        assert response.status_code == 422, response.text


def test_a_viewer_may_read_the_queue(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract, cipher)
    headers = auth(
        secured_client, tenant, make_user, role=UserRole.VIEWER, email="viewer@acme.test"
    )

    response = secured_client.get(QUEUE_PATH, headers=headers)

    assert response.status_code == 200, response.text


def test_the_queue_never_contains_a_value(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract, cipher)
    headers = auth(secured_client, tenant, make_user)

    body = secured_client.get(QUEUE_PATH, headers=headers).text

    # Neither the plaintext nor the ciphertext: the queue lists review *state*,
    # and reading a value is the audited reveal (D8-04).
    assert PLAINTEXT not in body
    assert cipher.encrypt(PLAINTEXT) not in body


def test_reading_the_queue_requires_a_token(secured_client: TestClient) -> None:
    assert secured_client.get(QUEUE_PATH).status_code == 401


# --- paging (gap 16) ------------------------------------------------------
def seed_queue(
    session_factory: sessionmaker[Session],
    contract: Contract,
    cipher: FieldCipher,
    *,
    count: int,
    confidence: float = 0.3,
    created_at: datetime | None = None,
) -> list[int]:
    """Store ``count`` pending fields on one contract and return their ids in queue order.

    The table allows one row per ``(contract, field_name)``, so each row takes the
    next enum member — which is also what keeps the ids increasing in queue order
    while every other sort term ties.
    """
    names = list(FieldName)
    assert count <= len(names), "the queue seed needs a distinct field name per row"
    return [
        add_field(
            session_factory,
            contract,
            cipher,
            field_name=names[index],
            value=PLAINTEXT if index == 0 else f"value-{index}",
            confidence=confidence,
            created_at=created_at,
        )
        for index in range(count)
    ]


def page_through(
    client: TestClient,
    headers: dict[str, str],
    *,
    limit: int,
) -> tuple[list[int], int]:
    """Follow ``next_cursor`` to the end and return (every field id seen, page count)."""
    seen: list[int] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        response = client.get(QUEUE_PATH, params=params, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        pages += 1
        seen.extend(item["field_id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return seen, pages
        assert pages < 20, "the cursor did not terminate"


def test_a_queue_longer_than_the_limit_is_reachable_page_by_page(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    # Seven rows against a page size of three: before the cursor existed, the
    # queue was capped at one page and everything past item three was invisible.
    ids = [
        add_field(
            session_factory,
            contract,
            cipher,
            field_name=list(FieldName)[index],
            value=PLAINTEXT if index == 0 else f"value-{index}",
            confidence=0.1 * (index + 1),
        )
        for index in range(7)
    ]
    headers = auth(secured_client, tenant, make_user)

    seen, pages = page_through(secured_client, headers, limit=3)

    assert seen == ids, "every queued field is reachable, worst confidence first"
    assert len(seen) == len(set(seen)), "a cursor must not repeat a row"
    assert pages == 3


def test_a_cursor_resumes_by_sort_key_not_by_offset(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    ids = seed_queue(session_factory, contract, cipher, count=6)
    headers = auth(secured_client, tenant, make_user)

    first = secured_client.get(QUEUE_PATH, params={"limit": 3}, headers=headers).json()
    assert [item["field_id"] for item in first["items"]] == ids[:3]
    assert first["next_cursor"] is not None

    # A reviewer decides one of the rows the first page already returned, which
    # takes it out of the queue and shifts every later row up by one. An
    # ``OFFSET 3`` would now hand back rows 5 and 6 of the original queue and
    # silently skip row 4 — a field nobody ever saw.
    decided = secured_client.patch(
        FIELD_PATH.format(field_id=ids[1]),
        json={"action": "accept", "version": 1},
        headers=headers,
    )
    assert decided.status_code == 200, decided.text

    second = secured_client.get(
        QUEUE_PATH,
        params={"limit": 3, "cursor": first["next_cursor"]},
        headers=headers,
    ).json()

    assert [item["field_id"] for item in second["items"]] == ids[3:]
    assert second["next_cursor"] is None


def test_a_tied_confidence_falls_through_to_creation_time_and_id(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    # Every sort term except the id ties here, so a cursor that compared only
    # confidence would either loop on the first row or skip its tied neighbours.
    pinned = datetime(2026, 1, 1, 12, 0, 0)
    ids = seed_queue(session_factory, contract, cipher, count=4, confidence=0.3, created_at=pinned)
    headers = auth(secured_client, tenant, make_user)

    seen, pages = page_through(secured_client, headers, limit=1)

    assert seen == ids
    assert pages == 4


def test_the_last_page_hands_back_no_cursor(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    seed_queue(session_factory, contract, cipher, count=2)
    headers = auth(secured_client, tenant, make_user)

    body = secured_client.get(QUEUE_PATH, params={"limit": 2}, headers=headers).json()

    # Exactly one full page and nothing behind it: a cursor here would make every
    # client spend a round trip discovering an empty page.
    assert body["count"] == 2
    assert body["next_cursor"] is None


@pytest.mark.parametrize(
    "cursor",
    [
        pytest.param("not-base64-%%%", id="not-base64"),
        pytest.param(base64.urlsafe_b64encode(b"{}").decode().rstrip("="), id="not-a-cursor"),
        pytest.param(
            base64.urlsafe_b64encode(b'{"v":2,"c":"0.5","t":"2026-01-01T00:00:00","i":1}')
            .decode()
            .rstrip("="),
            id="future-version",
        ),
        pytest.param(
            base64.urlsafe_b64encode(b'{"v":1,"c":"0.5","t":"2026-01-01T00:00:00"}')
            .decode()
            .rstrip("="),
            id="no-field-id",
        ),
    ],
)
def test_a_malformed_cursor_is_refused(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
    cursor: str,
) -> None:
    seed_queue(session_factory, contract, cipher, count=1)
    headers = auth(secured_client, tenant, make_user)

    response = secured_client.get(QUEUE_PATH, params={"cursor": cursor}, headers=headers)

    # A 422 rather than a silent first page: a client paging by cursors would
    # otherwise loop over page one forever and call it a server bug.
    assert response.status_code == 422, response.text
    assert "cursor is not recognised" in response.json()["error"]["message"]


def test_a_cursor_from_another_tenant_cannot_reach_their_queue(
    secured_client: TestClient,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    other_tenant: Tenant,
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    with session_scope(session_factory) as session:
        foreign = Contract(tenant_id=other_tenant.id, customer_ref="Globex")
        session.add(foreign)
        session.flush()
    foreign_ids = seed_queue(session_factory, foreign, cipher, count=2)
    headers = auth(secured_client, tenant, make_user)

    # The cursor is deliberately unsigned and carries no tenant (D8-06), so the
    # only thing standing between it and another organisation's backlog is the
    # query-level tenant predicate. Forge exactly that situation.
    foreign_cursor = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"v": 1, "c": "0.300", "t": "2000-01-01T00:00:00", "i": foreign_ids[0]}
            ).encode()
        )
        .decode()
        .rstrip("=")
    )

    body = secured_client.get(QUEUE_PATH, params={"cursor": foreign_cursor}, headers=headers).json()

    assert body["items"] == []
    assert body["next_cursor"] is None


# --- deciding -------------------------------------------------------------
def test_accepting_a_field_approves_it_and_clears_the_block(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    field_id = add_field(session_factory, contract, cipher)
    headers = auth(secured_client, tenant, make_user)

    response = secured_client.patch(
        FIELD_PATH.format(field_id=field_id),
        json={"action": "accept", "version": 1},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["item"]["status"] == "approved"
    assert body["item"]["blocks_pipeline"] is False
    assert body["item"]["version"] == 2
    # Zero means the pipeline may proceed for this contract.
    assert body["blocking"] == 0

    stored = load_field(session_factory, field_id)
    assert stored.status is FieldStatus.APPROVED
    assert stored.reviewed_by is not None
    assert stored.reviewed_at is not None
    assert stored.review_reason == "value accepted by reviewer"


def test_rejecting_a_field_keeps_it_blocking(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    field_id = add_field(session_factory, contract, cipher)
    headers = auth(secured_client, tenant, make_user)

    body = secured_client.patch(
        FIELD_PATH.format(field_id=field_id),
        json={"action": "reject", "version": 1},
        headers=headers,
    ).json()

    # A rejection is a decision that the value cannot be used, which is still a
    # hard stop — ``blocks_pipeline`` covers pending *and* rejected on purpose.
    assert body["item"]["status"] == "rejected"
    assert body["item"]["blocks_pipeline"] is True
    assert body["blocking"] == 1


def test_marking_a_field_unreadable_stops_it_blocking(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    field_id = add_field(session_factory, contract, cipher)
    headers = auth(secured_client, tenant, make_user)

    body = secured_client.patch(
        FIELD_PATH.format(field_id=field_id),
        json={"action": "mark_unreadable", "version": 1},
        headers=headers,
    ).json()

    # "The document does not contain this" is an answer, so it unblocks: the
    # contract can proceed without the field rather than waiting forever.
    assert body["item"]["status"] == "unreadable"
    assert body["item"]["blocks_pipeline"] is False
    assert body["blocking"] == 0


def test_a_reviewer_may_supply_their_own_reason(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    field_id = add_field(session_factory, contract, cipher)
    headers = auth(secured_client, tenant, make_user)

    body = secured_client.patch(
        FIELD_PATH.format(field_id=field_id),
        json={"action": "reject", "version": 1, "reason": "the scan is illegible"},
        headers=headers,
    ).json()

    assert body["item"]["review_reason"] == "the scan is illegible"


def test_editing_stores_the_corrected_value_normalized_and_encrypted(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    field_id = add_field(
        session_factory, contract, cipher, value="wrong@example.test", confidence=0.3
    )
    headers = auth(secured_client, tenant, make_user)

    response = secured_client.patch(
        FIELD_PATH.format(field_id=field_id),
        json={"action": "edit", "version": 1, "value": "  A.Rahman@Example.TEST "},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["item"]["status"] == "approved"

    stored = load_field(session_factory, field_id)
    # Normalized for the field it belongs to, not stored as typed: an
    # unparseable correction would pass review and fail at send time.
    assert stored.normalized_value is not None
    assert "a.rahman@example.test" not in stored.normalized_value
    assert cipher.decrypt(stored.normalized_value) == "a.rahman@example.test"
    # Encrypted at rest, exactly like an extracted value (D7-01).
    assert stored.raw_value != "A.Rahman@Example.TEST"
    assert cipher.decrypt(stored.raw_value or "") == "A.Rahman@Example.TEST"
    # The score describes the extraction, not the correction (D8-02).
    assert float(stored.confidence) == pytest.approx(0.3)


def test_an_unparseable_correction_is_refused_and_changes_nothing(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    field_id = add_field(
        session_factory, contract, cipher, field_name=FieldName.PHONE, value="+442079460958"
    )
    headers = auth(secured_client, tenant, make_user)

    response = secured_client.patch(
        FIELD_PATH.format(field_id=field_id),
        json={"action": "edit", "version": 1, "value": "call the office"},
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json()["error"]["message"] == "The value is not a valid phone"
    # The rejected text is not echoed back — an error string is the easiest way
    # for PII to reach a log.
    assert "call the office" not in response.text

    stored = load_field(session_factory, field_id)
    assert stored.status is FieldStatus.PENDING_REVIEW
    assert stored.normalized_value is not None
    assert cipher.decrypt(stored.normalized_value) == "+442079460958"
    assert stored.version == 1
    assert audit_rows(session_factory) == []


def test_a_contact_correction_is_findable_by_its_new_value(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    """The digest follows the corrected value, so a STOP for the new number matches.

    Quiet failure is what this prevents: with a stale digest the correction looks
    saved, the customer's opt-out keeps matching the number they no longer own, and
    an inbound STOP from the new one finds nothing.
    """
    field_id = add_field(
        session_factory, contract, cipher, field_name=FieldName.PHONE, value="+442079460958"
    )
    headers = auth(secured_client, tenant, make_user)

    response = secured_client.patch(
        FIELD_PATH.format(field_id=field_id),
        json={"action": "edit", "version": 1, "value": "+44 20 7946 1234"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    stored = load_field(session_factory, field_id)
    assert stored.blind_index == ContactBlindIndex(BLIND_INDEX_KEY).digest("+442079461234")
    # The digest is of the normalized form, not of what was typed.
    assert stored.blind_index != ContactBlindIndex(BLIND_INDEX_KEY).digest("+44 20 7946 1234")


def test_correcting_a_date_needs_no_contact_index(
    sealed_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    """A deployment with no inbound SMS must still be able to review a date.

    The review queue is the gate that unblocks every notification, so coupling a
    date correction to a key that only matters for inbound replies would be an
    availability bug dressed up as a security rule.
    """
    field_id = add_field(
        session_factory,
        contract,
        cipher,
        field_name=FieldName.START_DATE,
        value="2025-01-01",
        confidence=0.3,
    )
    headers = auth(sealed_client, tenant, make_user)

    response = sealed_client.patch(
        FIELD_PATH.format(field_id=field_id),
        json={"action": "edit", "version": 1, "value": "2026-03-01"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    stored = load_field(session_factory, field_id)
    assert cipher.decrypt(stored.normalized_value or "") == "2026-03-01"
    # A date cannot arrive as an inbound reply, so it carries no digest at all.
    assert stored.blind_index is None


def test_correcting_a_contact_value_without_an_index_key_changes_nothing(
    sealed_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    """No key means no digest, and a contact value without one cannot be found.

    Storing it anyway would look like success while an inbound STOP for that
    address matched nothing, so the correction is refused whole — including the
    two columns that would otherwise have been written.
    """
    field_id = add_field(session_factory, contract, cipher, value="wrong@example.test")
    headers = auth(sealed_client, tenant, make_user)

    response = sealed_client.patch(
        FIELD_PATH.format(field_id=field_id),
        json={"action": "edit", "version": 1, "value": "right@example.test"},
        headers=headers,
    )

    assert response.status_code == 503
    stored = load_field(session_factory, field_id)
    assert cipher.decrypt(stored.normalized_value or "") == "wrong@example.test"
    assert stored.status is FieldStatus.PENDING_REVIEW
    assert stored.version == 1
    assert audit_rows(session_factory) == []


def test_a_stale_version_is_a_conflict_carrying_the_current_row(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    field_id = add_field(session_factory, contract, cipher)
    headers = auth(secured_client, tenant, make_user)

    response = secured_client.patch(
        FIELD_PATH.format(field_id=field_id),
        json={"action": "accept", "version": 99},
        headers=headers,
    )

    assert response.status_code == 409
    error = response.json()["error"]
    # The winning row travels with the conflict so the reviewer can see what
    # changed instead of only being told they lost the race.
    assert error["details"]["current"]["field_id"] == field_id
    assert error["details"]["current"]["version"] == 1
    assert error["details"]["current"]["status"] == "pending_review"
    # Nothing was written by the losing request.
    assert load_field(session_factory, field_id).status is FieldStatus.PENDING_REVIEW
    assert audit_rows(session_factory) == []


def test_a_decision_writes_one_audit_row(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    field_id = add_field(session_factory, contract, cipher)
    user = make_user(tenant, email=EMAIL, role=UserRole.OPERATOR)
    headers = login(secured_client, tenant, EMAIL)

    secured_client.patch(
        FIELD_PATH.format(field_id=field_id),
        json={"action": "accept", "version": 1},
        headers=headers,
    )

    rows = audit_rows(session_factory)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "field.reviewed"
    assert row.entity == "extracted_fields"
    assert row.entity_id == str(field_id)
    assert row.actor_type == "user"
    assert row.actor_id == user.id
    assert row.tenant_id == tenant.id
    assert row.reason == "value accepted by reviewer"
    assert len(row.correlation_id) == 36
    # Snapshots describe the transition, which is what makes the log evidence.
    before = json.loads(row.before_state or "{}")
    after = json.loads(row.after_state or "{}")
    assert (before["status"], before["version"]) == ("pending_review", 1)
    assert (after["status"], after["version"]) == ("approved", 2)


def test_the_audit_trail_never_contains_the_value(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    field_id = add_field(session_factory, contract, cipher, value="wrong@example.test")
    headers = auth(secured_client, tenant, make_user)

    secured_client.patch(
        FIELD_PATH.format(field_id=field_id),
        json={"action": "edit", "version": 1, "value": PLAINTEXT},
        headers=headers,
    )

    row = audit_rows(session_factory)[0]
    # ``AuditLog`` has no column encryption and is readable by any operator, so
    # storing the corrected value here would undo the encryption at rest.
    for column in (row.before_state, row.after_state, row.reason):
        assert PLAINTEXT not in (column or "")


def test_a_viewer_may_not_decide_anything(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    field_id = add_field(session_factory, contract, cipher)
    headers = auth(
        secured_client, tenant, make_user, role=UserRole.VIEWER, email="viewer@acme.test"
    )

    response = secured_client.patch(
        FIELD_PATH.format(field_id=field_id),
        json={"action": "accept", "version": 1},
        headers=headers,
    )

    # This is the write that can eventually put a message in front of a customer,
    # so the F6/D6-01 operator-only rule applies to it as well.
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    assert load_field(session_factory, field_id).status is FieldStatus.PENDING_REVIEW
    assert audit_rows(session_factory) == []


def test_a_field_of_another_tenant_is_not_found(
    secured_client: TestClient,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    other_tenant: Tenant,
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    with session_scope(session_factory) as session:
        foreign = Contract(tenant_id=other_tenant.id, customer_ref="Globex")
        session.add(foreign)
        session.flush()
    field_id = add_field(session_factory, foreign, cipher)
    headers = auth(secured_client, tenant, make_user)

    response = secured_client.patch(
        FIELD_PATH.format(field_id=field_id),
        json={"action": "accept", "version": 1},
        headers=headers,
    )

    # 404 rather than 403: a 403 would confirm the id exists somewhere.
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert load_field(session_factory, field_id).status is FieldStatus.PENDING_REVIEW
    assert audit_rows(session_factory) == []


def test_an_unknown_field_is_not_found(
    secured_client: TestClient,
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    headers = auth(secured_client, tenant, make_user)

    response = secured_client.patch(
        FIELD_PATH.format(field_id=987654),
        json={"action": "accept", "version": 1},
        headers=headers,
    )

    assert response.status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "approve", "version": 1},
        {"action": "accept", "version": 0},
        {"action": "accept", "version": 1, "value": PLAINTEXT},
        {"action": "edit", "version": 1},
        {"action": "edit", "version": 1, "value": "   "},
    ],
)
def test_a_malformed_decision_is_refused_by_the_schema(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
    payload: dict[str, Any],
) -> None:
    field_id = add_field(session_factory, contract, cipher)
    headers = auth(secured_client, tenant, make_user)

    response = secured_client.patch(
        FIELD_PATH.format(field_id=field_id), json=payload, headers=headers
    )

    # An unknown action or a value where none is stored is a 422 from the schema,
    # not a silent no-op: a caller sending a value with ``accept`` believes it is
    # being saved.
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"


def test_deciding_requires_a_token(secured_client: TestClient, contract: Contract) -> None:
    response = secured_client.patch(
        FIELD_PATH.format(field_id=1), json={"action": "accept", "version": 1}
    )

    assert response.status_code == 401


# --- revealing ------------------------------------------------------------
def test_revealing_a_value_returns_the_plaintext(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    field_id = add_field(session_factory, contract, cipher)
    headers = auth(secured_client, tenant, make_user)

    response = secured_client.post(REVEAL_PATH.format(field_id=field_id), headers=headers)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "field_id": field_id,
        "field_name": "email",
        "value": PLAINTEXT,
        "version": 1,
    }


def test_revealing_writes_exactly_one_audit_row(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    field_id = add_field(session_factory, contract, cipher)
    user = make_user(tenant, email=EMAIL, role=UserRole.OPERATOR)
    headers = login(secured_client, tenant, EMAIL)

    secured_client.post(REVEAL_PATH.format(field_id=field_id), headers=headers)

    rows = audit_rows(session_factory)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "field.value_revealed"
    assert row.entity == "extracted_fields"
    assert row.entity_id == str(field_id)
    assert row.actor_id == user.id
    # A reveal has no before/after: it changes nothing, it observes something.
    assert row.before_state is None
    assert row.after_state is None
    assert row.reason is None
    # ...and the value itself is not in the trail, only the fact of the reveal.
    assert PLAINTEXT not in (row.before_state or "") + (row.after_state or "")


def test_revealing_without_a_configured_key_is_unavailable(
    client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    field_id = add_field(session_factory, contract, cipher)
    headers = auth(client, tenant, make_user)

    response = client.post(REVEAL_PATH.format(field_id=field_id), headers=headers)

    # Fail closed: an installation with no key cannot decrypt, and must say so
    # rather than return an empty value that looks like "nothing stored".
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert PLAINTEXT not in response.text
    assert audit_rows(session_factory) == []


def test_a_field_with_no_stored_value_cannot_be_revealed(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    field_id = add_field(session_factory, contract, cipher, value=None)
    headers = auth(secured_client, tenant, make_user)

    response = secured_client.post(REVEAL_PATH.format(field_id=field_id), headers=headers)

    assert response.status_code == 409
    assert "no stored value" in response.json()["error"]["message"]


def test_a_value_encrypted_with_another_key_cannot_be_revealed(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    foreign_cipher = FieldCipher(Fernet.generate_key().decode("ascii"))
    field_id = add_field(session_factory, contract, cipher)
    rotated = foreign_cipher.encrypt(PLAINTEXT)
    set_raw_value(session_factory, field_id, rotated)
    headers = auth(secured_client, tenant, make_user)

    response = secured_client.post(REVEAL_PATH.format(field_id=field_id), headers=headers)

    # Almost always a rotated key rather than corruption, and neither the token
    # nor the exception text is echoed — both can name key material.
    assert response.status_code == 500
    assert rotated not in response.text
    assert PLAINTEXT not in response.text


def test_a_viewer_may_not_reveal_a_value(
    secured_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    field_id = add_field(session_factory, contract, cipher)
    headers = auth(
        secured_client, tenant, make_user, role=UserRole.VIEWER, email="viewer@acme.test"
    )

    response = secured_client.post(REVEAL_PATH.format(field_id=field_id), headers=headers)

    # Reading a customer's contact details is a PII access event, not a view of
    # review state.
    assert response.status_code == 403
    assert PLAINTEXT not in response.text
    assert audit_rows(session_factory) == []


def test_a_field_of_another_tenant_cannot_be_revealed(
    secured_client: TestClient,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    other_tenant: Tenant,
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    with session_scope(session_factory) as session:
        foreign = Contract(tenant_id=other_tenant.id, customer_ref="Globex")
        session.add(foreign)
        session.flush()
    field_id = add_field(session_factory, foreign, cipher)
    headers = auth(secured_client, tenant, make_user)

    response = secured_client.post(REVEAL_PATH.format(field_id=field_id), headers=headers)

    assert response.status_code == 404
    assert PLAINTEXT not in response.text
    assert audit_rows(session_factory) == []


def test_revealing_requires_a_token(secured_client: TestClient, contract: Contract) -> None:
    assert secured_client.post(REVEAL_PATH.format(field_id=1)).status_code == 401


# --- approving an end date writes the contract too (D10-96) ----------------
#
# ``Contract.end_date`` is what the reminder planner reads: ``build_notification``
# computes every milestone from that column and refuses ``MISSING_END_DATE`` when it
# is empty. For a contract uploaded as a document the date arrives as an extracted
# field instead, so before D10-96 the whole product promise was unreachable: a
# reviewer approved the date, and the pipeline still saw nothing to remind about.
#
# These tests fail against that build. The factories in this file hand ``end_date``
# straight to ``Contract``, so nothing else in the suite ever exercised the seam.

END_EXTRACTED = date(2026, 3, 31)
END_CORRECTED = date(2026, 5, 4)


@dataclass(frozen=True, slots=True)
class _PlannedSend:
    """The two attributes :func:`build_notification` reads off a decision.

    A stub rather than an ``AgentDecision`` row: the planner takes ``Any`` on
    purpose, because the policy needs a channel and a review flag and nothing else.
    A DB row here would add three imports and a session to a test about review.
    """

    channel: Channel = Channel.EMAIL
    needs_review: bool = False


def test_accepting_an_end_date_writes_it_onto_the_contract(
    sealed_client: TestClient,
    undated_contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    field_id = add_field(
        session_factory,
        undated_contract,
        cipher,
        field_name=FieldName.END_DATE,
        value=END_EXTRACTED.isoformat(),
    )
    headers = auth(sealed_client, tenant, make_user)

    response = sealed_client.patch(
        FIELD_PATH.format(field_id=field_id),
        json={"action": "accept", "version": 1},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert load_contract(session_factory, undated_contract.id).end_date == END_EXTRACTED
    # The response echoes the promoted date, so a reviewer sees the same value the
    # scan will read rather than a field that only *looks* approved.
    assert response.json()["item"]["end_date"] == END_EXTRACTED.isoformat()


def test_accepting_an_end_date_replaces_a_date_the_contract_already_had(
    sealed_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    """A guessed ingestion parameter loses to the date a person approved.

    ``contract`` carries 2026-01-01. The document says 2026-03-31, and the reviewer
    agreeing with the document has to be able to correct the guess.
    """
    field_id = add_field(
        session_factory,
        contract,
        cipher,
        field_name=FieldName.END_DATE,
        value=END_EXTRACTED.isoformat(),
    )
    headers = auth(sealed_client, tenant, make_user)

    response = sealed_client.patch(
        FIELD_PATH.format(field_id=field_id),
        json={"action": "accept", "version": 1},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert load_contract(session_factory, contract.id).end_date == END_EXTRACTED


def test_a_correction_promotes_the_corrected_date_not_the_extracted_one(
    sealed_client: TestClient,
    undated_contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    field_id = add_field(
        session_factory,
        undated_contract,
        cipher,
        field_name=FieldName.END_DATE,
        value=END_EXTRACTED.isoformat(),
    )
    headers = auth(sealed_client, tenant, make_user)

    response = sealed_client.patch(
        FIELD_PATH.format(field_id=field_id),
        json={"action": "edit", "version": 1, "value": END_CORRECTED.isoformat()},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    # The contract takes what the reviewer typed, not what the regex found.
    assert load_contract(session_factory, undated_contract.id).end_date == END_CORRECTED
    stored = load_field(session_factory, field_id)
    assert stored.normalized_value is not None
    assert cipher.decrypt(stored.normalized_value) == END_CORRECTED.isoformat()


@pytest.mark.parametrize("action", ["reject", "mark_unreadable"])
def test_a_decision_that_is_not_an_approval_leaves_the_contract_alone(
    action: str,
    sealed_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    field_id = add_field(
        session_factory,
        contract,
        cipher,
        field_name=FieldName.END_DATE,
        value=END_EXTRACTED.isoformat(),
    )
    headers = auth(sealed_client, tenant, make_user)

    response = sealed_client.patch(
        FIELD_PATH.format(field_id=field_id),
        json={"action": action, "version": 1},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert load_contract(session_factory, contract.id).end_date == date(2026, 1, 1)
    row = audit_rows(session_factory)[0]
    # Nothing was promoted, so the snapshot carries no contract keys at all — a
    # reviewer saying "this is not the end date" must not move the date either way.
    assert "contract_end_date" not in json.loads(row.after_state or "{}")


def test_approving_a_start_date_does_not_touch_the_contract(
    sealed_client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    """Only ``end_date`` is promoted, and that is a decision, not an oversight.

    A start date has no column to land in, and inferring one from the other would
    invent a fact the document may not contain.
    """
    field_id = add_field(
        session_factory,
        contract,
        cipher,
        field_name=FieldName.START_DATE,
        value="2025-01-01",
    )
    headers = auth(sealed_client, tenant, make_user)

    response = sealed_client.patch(
        FIELD_PATH.format(field_id=field_id),
        json={"action": "accept", "version": 1},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert load_contract(session_factory, contract.id).end_date == date(2026, 1, 1)
    row = audit_rows(session_factory)[0]
    assert "contract_end_date" not in json.loads(row.after_state or "{}")


def test_an_end_date_that_will_not_decrypt_is_still_decided(
    sealed_client: TestClient,
    undated_contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    """A rotated ``ENCRYPTION_KEY`` must not leave the reviewer stuck.

    The row was sealed under a key this deployment no longer holds. The decision is
    worth recording — a human did look at it — but nothing can be promoted, and the
    route must say so by leaving the date alone rather than by raising.
    """
    field_id = add_field(
        session_factory,
        undated_contract,
        cipher,
        field_name=FieldName.END_DATE,
        value=None,
    )
    foreign = FieldCipher(Fernet.generate_key().decode()).encrypt(END_EXTRACTED.isoformat())
    set_raw_value(session_factory, field_id, foreign)
    # ``set_raw_value`` is an UPDATE, so it moves the row's optimistic-lock version;
    # sending a stale one here would test the version check instead of the decision.
    version = load_field(session_factory, field_id).version
    headers = auth(sealed_client, tenant, make_user)

    response = sealed_client.patch(
        FIELD_PATH.format(field_id=field_id),
        json={"action": "accept", "version": version},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert load_field(session_factory, field_id).status is FieldStatus.APPROVED
    assert load_contract(session_factory, undated_contract.id).end_date is None
    assert foreign not in response.text


def test_the_promoted_date_is_recorded_in_the_field_audit_row(
    sealed_client: TestClient,
    undated_contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    """One row, not two: promoting is part of the decision, not a second action.

    The date itself is not PII, and it is the fact an operator investigating a wrong
    reminder needs, so it belongs in the snapshot beside the field's own state.
    """
    field_id = add_field(
        session_factory,
        undated_contract,
        cipher,
        field_name=FieldName.END_DATE,
        value=END_EXTRACTED.isoformat(),
    )
    headers = auth(sealed_client, tenant, make_user)

    sealed_client.patch(
        FIELD_PATH.format(field_id=field_id),
        json={"action": "accept", "version": 1},
        headers=headers,
    )

    rows = audit_rows(session_factory)
    assert len(rows) == 1
    before = json.loads(rows[0].before_state or "{}")
    after = json.loads(rows[0].after_state or "{}")
    assert before["contract_end_date"] is None
    assert after["contract_end_date"] == END_EXTRACTED.isoformat()
    assert after["status"] == FieldStatus.APPROVED.value


def test_an_approved_end_date_unblocks_the_reminder_pipeline(
    sealed_client: TestClient,
    undated_contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    settings: Settings,
    tenant: Tenant,
) -> None:
    """The assertion that would have caught the hole: the planner, end to end.

    Approving the field and then asking ``build_notification`` the question the
    scheduler asks is the only way to see that the two halves join up — a test of
    the route alone passes while the product refuses every milestone.
    """
    end = utc_now().date() + timedelta(days=40)
    field_id = add_field(
        session_factory,
        undated_contract,
        cipher,
        field_name=FieldName.END_DATE,
        value=end.isoformat(),
    )
    decision = _PlannedSend()

    with pytest.raises(DispatchRefusedError) as refused:
        build_notification(undated_contract, decision, now=utc_now(), settings=settings)
    assert refused.value.code == REFUSAL_MISSING_END_DATE

    headers = auth(sealed_client, tenant, make_user)
    decided = sealed_client.patch(
        FIELD_PATH.format(field_id=field_id),
        json={"action": "accept", "version": 1},
        headers=headers,
    )
    assert decided.status_code == 200, decided.text

    notification = build_notification(
        load_contract(session_factory, undated_contract.id),
        decision,
        now=utc_now(),
        settings=settings,
        fields=[load_field(session_factory, field_id)],
    )
    assert notification.template == "T-60"
    assert notification.scheduled_date == end - timedelta(days=60)
