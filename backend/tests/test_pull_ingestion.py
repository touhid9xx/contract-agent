"""Pull-mode ingestion (P2-H): the one route where the API opens the connection.

Every other ingestion route answers a request a client sends. ``POST
/contracts/pull`` names a URL and reaches out, so the tests here are about two
different kinds of guarantee, and they need different doubles.

**What the transport must refuse** is ``app.core.pull_source``'s job and is
tested through that module's own errors — a test that never opens a socket. The
socket is not faked *around* the class: ``StubSource`` subclasses ``PullSource``
and overrides only ``fetch``, so ``contracts_from`` is the real narrowing code.
The empty-answer and over-limit refusals are part of what a pull must get right,
and a hand-written double would be testing the double.

**What the route must do** with a source that answered is the rest: one row and
one outbox event per record, tenant from the token, all-or-nothing across the
batch, and an already-held record skipped rather than refused.

That last one is the only place a duplicate is not a 409, and it is why these
tests pull the same source twice rather than trusting the docstring: a source
returns its *whole* list every time, so a 409 would make the second pull of a
working integration fail forever.

The refusal tests assert ``contracts == []`` as well as the status code. A
refusal that had already written the records it read before the bad one would
pass a status-only assertion and leave an operator with exactly the half-copied
portfolio the all-or-nothing rule exists to prevent.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import pull_source
from app.core.config import Settings
from app.core.pull_source import (
    PulledPayload,
    PullPayloadError,
    PullSource,
    PullSourceError,
    PullTooLargeError,
    PullUnavailableError,
    PullUrlRefusedError,
)
from app.db.enums import ContractSource, OutboxStatus, UserRole
from app.db.models import Contract, OutboxEvent, Tenant
from app.db.session import session_scope
from app.main import create_app

PULL_PATH = "/api/v1/contracts/pull"
LOGIN_PATH = "/api/v1/auth/login"

#: Matches ``conftest.DEFAULT_PASSWORD``. Not a credential: it protects in-memory
#: databases that exist only for the duration of a test.
PASSWORD = "correct-horse-battery-staple"  # nosec B105

#: The URL every test names. It is never resolved — ``StubSource.fetch`` records it
#: and answers — which is the point: no test in this file opens a socket.
SOURCE_URL = "https://contracts.acme.test/api/v1/contracts"


# --- the double -----------------------------------------------------------
class StubSource(PullSource):
    """A ``PullSource`` whose socket is a field.

    ``document`` is what ``fetch`` answers with; ``error`` is what it raises
    instead. Setting both is a mistake the tests do not make, and ``fetch``
    prefers the error so a parametrised refusal case cannot accidentally import
    a fixture the previous test left behind.
    """

    def __init__(
        self,
        *,
        max_contracts: int = 50,
        document: object = None,
        error: PullSourceError | None = None,
    ) -> None:
        super().__init__(
            timeout=1.0,
            max_bytes=4096,
            allow_private_hosts=True,
            max_contracts=max_contracts,
        )
        self.document = document
        self.error = error
        #: Every URL ``fetch`` was asked for, in order. Asserted rather than the
        #: response's echo, because the echo could be right about a URL the source
        #: was never actually asked to fetch.
        self.urls: list[str] = []

    def fetch(self, url: str) -> PulledPayload:
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return PulledPayload(url=url, document=self.document, bytes_read=57)


# --- helpers --------------------------------------------------------------
def _record(**overrides: Any) -> dict[str, Any]:
    """One contract as a company API plausibly returns it.

    Deliberately carries no ``source``: the route forces ``api``, and a fixture
    that set it would hide the fact that a record cannot choose.
    """
    record: dict[str, Any] = {
        "customer_ref": "A. Rahman",
        "start_date": "2024-01-01",
        "end_date": "2026-01-01",
        "timezone": "Europe/London",
    }
    record.update(overrides)
    return record


def _token(client: TestClient, tenant: Tenant, user_email: str) -> str:
    response = client.post(
        LOGIN_PATH,
        json={"tenant": tenant.slug, "email": user_email, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def _pull(client: TestClient, token: str | None, *, url: str = SOURCE_URL) -> Response:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    return client.post(PULL_PATH, json={"url": url}, headers=headers)


def _contracts(session_factory: sessionmaker[Session]) -> list[Contract]:
    with session_scope(session_factory) as session:
        return list(session.execute(select(Contract).order_by(Contract.id)).scalars())


def _events(session_factory: sessionmaker[Session]) -> list[OutboxEvent]:
    with session_scope(session_factory) as session:
        return list(session.execute(select(OutboxEvent).order_by(OutboxEvent.id)).scalars())


def _error(response: Response) -> dict[str, Any]:
    """The ``error`` member of the app-wide envelope ``main.error_payload`` builds.

    Read from the response rather than built here: a refusal this module raised
    with its own key names would still pass a test that assembled the same dict.
    """

    error: dict[str, Any] = response.json()["error"]
    return error


def _code(response: Response) -> str:
    """The machine-readable refusal code the caller switches on."""

    return str(_error(response)["code"])


# --- fixtures -------------------------------------------------------------
@pytest.fixture
def pull_app(settings: Settings, session_factory: sessionmaker[Session]) -> FastAPI:
    """The real app, built here so the test holds the object it overrides.

    ``TestClient.app`` is typed as an ASGI app, which has no
    ``dependency_overrides``; holding the ``FastAPI`` instance avoids a cast and
    avoids reaching through the client for the one thing under test.
    """
    return create_app(settings, session_factory=session_factory)


@pytest.fixture
def source() -> StubSource:
    return StubSource()


@pytest.fixture
def pull_client(pull_app: FastAPI, source: StubSource) -> Iterator[TestClient]:
    pull_app.dependency_overrides[pull_source] = lambda: source
    with TestClient(pull_app) as client:
        yield client


@pytest.fixture
def token(pull_client: TestClient, tenant: Tenant, make_user: Callable[..., Any]) -> str:
    """An operator of the ``tenant`` fixture, already logged in."""
    make_user(tenant, email="ops@acme.test", role=UserRole.OPERATOR)
    return _token(pull_client, tenant, "ops@acme.test")


# --- the happy path -------------------------------------------------------
def test_a_pull_imports_every_contract_the_source_returns(
    pull_client: TestClient,
    token: str,
    source: StubSource,
    tenant: Tenant,
    session_factory: sessionmaker[Session],
) -> None:
    source.document = [
        _record(customer_ref="A. Rahman"),
        _record(customer_ref="B. Chowdhury", end_date="2027-03-31"),
    ]

    response = _pull(pull_client, token)

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["customer_ref"] for item in body["imported"]] == [
        "A. Rahman",
        "B. Chowdhury",
    ]
    assert body["skipped"] == []
    # The source was asked for the URL the caller named, once.
    assert source.urls == [SOURCE_URL]

    rows = _contracts(session_factory)
    assert len(rows) == 2
    # The tenant comes from the token and nothing else: the request body has no
    # field to put one in, and the route never reads a header for it.
    assert {row.tenant_id for row in rows} == {tenant.id}

    # One event per row, in the same transaction: the row and its announcement are
    # written or discarded together, which is what the outbox is for.
    events = _events(session_factory)
    assert len(events) == 2
    assert {event.event_type for event in events} == {"contract.ingested"}
    assert {event.status for event in events} == {OutboxStatus.PENDING}
    # The summary quotes the event's public id, not its row id: the first is what a
    # consumer will see on the bus, and the second is our own bookkeeping.
    assert [str(event.event_id) for event in events] == [
        item["outbox_event_id"] for item in body["imported"]
    ]


def test_bytes_read_is_reported_so_a_wrong_endpoint_looks_wrong(
    pull_client: TestClient,
    token: str,
    source: StubSource,
) -> None:
    source.document = [_record()]

    body = _pull(pull_client, token).json()

    # Not a contract count: a pull that read 57 bytes and imported one row is a
    # plausible pull, and a pull that read 4 KiB and imported nothing is a URL
    # pointing at the wrong place. The number the operator needs is this one.
    assert body["bytes_read"] == 57
    assert body["url"] == SOURCE_URL


def test_a_record_cannot_declare_the_source_it_arrived_by(
    pull_client: TestClient,
    token: str,
    source: StubSource,
    session_factory: sessionmaker[Session],
) -> None:
    """``source`` joins the dedupe fingerprint, so honouring it would let a source
    sidestep idempotency by labelling identical content differently.

    ``pdf`` is a *legal* ``ContractSource``, and that is the point: a claim that
    failed validation would be refused by pydantic and prove nothing about the
    route. The record is accepted and relabelled, so what this pins is that the
    route overrides a well-formed claim rather than that it rejects a bad one.
    """
    source.document = [_record(source=ContractSource.PDF.value)]

    response = _pull(pull_client, token)

    assert response.status_code == 200, response.text
    assert response.json()["imported"][0]["source"] == ContractSource.API.value
    assert [row.source for row in _contracts(session_factory)] == [ContractSource.API]


# --- idempotency ----------------------------------------------------------
def test_a_second_pull_of_the_same_source_skips_what_it_already_holds(
    pull_client: TestClient,
    token: str,
    source: StubSource,
    session_factory: sessionmaker[Session],
) -> None:
    """A source returns its whole list every time, so a re-sync is not a conflict.

    Refusing with 409 — which is what ``POST /contracts`` does for a retried
    single write — would make the second pull of a working integration fail
    forever, and the operator's only recourse would be to stop pulling.
    """
    source.document = [_record(customer_ref="A. Rahman"), _record(customer_ref="B. Chowdhury")]
    first = _pull(pull_client, token)
    assert first.status_code == 200, first.text
    held = [item["id"] for item in first.json()["imported"]]

    second = _pull(pull_client, token)

    assert second.status_code == 200, second.text
    body = second.json()
    assert body["imported"] == []
    assert [item["contract_id"] for item in body["skipped"]] == held

    # Nothing was written the second time: the row count and the event count are
    # what a "skipped" claim means, and the response alone cannot prove them.
    assert len(_contracts(session_factory)) == 2
    assert len(_events(session_factory)) == 2


def test_two_identical_records_in_one_pull_import_once(
    pull_client: TestClient,
    token: str,
    source: StubSource,
    session_factory: sessionmaker[Session],
) -> None:
    """The per-record flush is what makes the second record see the first."""
    source.document = [_record(), _record()]

    response = _pull(pull_client, token)

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["imported"]) == 1
    assert len(body["skipped"]) == 1
    assert body["skipped"][0]["contract_id"] == body["imported"][0]["id"]
    assert len(_contracts(session_factory)) == 1


# --- all or nothing -------------------------------------------------------
def test_one_invalid_record_imports_none_of_the_others(
    pull_client: TestClient,
    token: str,
    source: StubSource,
    session_factory: sessionmaker[Session],
) -> None:
    source.document = [
        _record(customer_ref="A. Rahman"),
        _record(customer_ref="B. Chowdhury", start_date="2026-01-01", end_date="2024-01-01"),
        _record(customer_ref="C. Haque"),
    ]

    response = _pull(pull_client, token)

    assert response.status_code == 422, response.text
    assert _code(response) == "PULL_RECORD_INVALID"
    detail = _error(response)["details"]
    # Position, so the operator can find the record in the source's own output.
    assert detail["contract"] == 2
    # And the field, not the value: pydantic echoes the offending input, and these
    # are whatever a customer's API chose to send.
    fields = {error["field"] for error in detail["errors"]}
    # The date-order rule is a ``model_validator``, so pydantic hands it an empty
    # ``loc`` and no single field is to blame — ``record`` says exactly that
    # instead of an empty string that would read as a mapping bug. The message is
    # where the two disagreeing dates are named, so it is asserted too.
    assert fields == {"record"}
    messages = " ".join(error["message"] for error in detail["errors"])
    assert "end_date" in messages
    assert "2024-01-01" not in response.text

    assert _contracts(session_factory) == []
    assert _events(session_factory) == []


# --- what the source may answer -------------------------------------------
def test_an_empty_answer_is_a_refusal_not_an_empty_import(
    pull_client: TestClient,
    token: str,
    source: StubSource,
    session_factory: sessionmaker[Session],
) -> None:
    """ "0 imported" reported as success is how a wrong URL looks like a working one."""
    source.document = []

    response = _pull(pull_client, token)

    assert response.status_code == 422, response.text
    assert _code(response) == "PULL_PAYLOAD_INVALID"
    assert _contracts(session_factory) == []


def test_a_source_answering_too_many_contracts_is_refused(
    pull_app: FastAPI,
    pull_client: TestClient,
    token: str,
    session_factory: sessionmaker[Session],
) -> None:
    """The cap is a property of the source, not of the route, so it is replaced here."""
    pull_app.dependency_overrides[pull_source] = lambda: StubSource(
        max_contracts=2,
        document=[_record(customer_ref=ref) for ref in ("A", "B", "C")],
    )

    response = _pull(pull_client, token)

    assert response.status_code == 422, response.text
    assert _code(response) == "PULL_PAYLOAD_INVALID"
    assert _contracts(session_factory) == []


def test_a_body_that_is_not_contracts_is_refused(
    pull_app: FastAPI,
    pull_client: TestClient,
    token: str,
    session_factory: sessionmaker[Session],
) -> None:
    pull_app.dependency_overrides[pull_source] = lambda: StubSource(
        document={"unexpected": "shape"}
    )

    response = _pull(pull_client, token)

    # A dict with no ``contracts`` member is read as one contract, which then fails
    # required-field validation — refused, and refused for the right reason.
    assert response.status_code == 422, response.text
    assert _code(response) == "PULL_RECORD_INVALID"
    assert _contracts(session_factory) == []


# --- what the fetch raises, and where each one lands -----------------------
@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        # The caller's field is wrong and well-formed: 422, and the message names
        # the host and the address it was refused for, never a credential.
        (PullUrlRefusedError("the host 'internal' is not public"), 422, "PULL_URL_REFUSED"),
        (
            PullPayloadError("the source's body is not JSON: Expecting value"),
            422,
            "PULL_PAYLOAD_INVALID",
        ),
        # Our process is fine, the thing we were told to reach is not. Not 504: a
        # timeout is one of several causes and they do not all come from upstream.
        (
            PullUnavailableError("the source answered 500 (Internal Server Error)"),
            502,
            "PULL_UNAVAILABLE",
        ),
        # 413 for the same reason the document route answers 413: the request is
        # valid, and the client's only problem is size.
        (PullTooLargeError(size=2048, limit=1024), 413, "PULL_TOO_LARGE"),
    ],
)
def test_every_way_a_fetch_fails_lands_where_the_caller_can_act_on_it(
    pull_client: TestClient,
    token: str,
    source: StubSource,
    session_factory: sessionmaker[Session],
    error: PullSourceError,
    status_code: int,
    code: str,
) -> None:
    source.error = error

    response = _pull(pull_client, token)

    assert response.status_code == status_code, response.text
    assert _code(response) == code
    assert _contracts(session_factory) == []
    assert _events(session_factory) == []


def test_the_size_refusal_carries_both_numbers(
    pull_client: TestClient,
    token: str,
    source: StubSource,
) -> None:
    """A bare "too large" leaves the operator guessing which of the two to change."""
    source.error = PullTooLargeError(size=2048, limit=1024)

    response = _pull(pull_client, token)

    details = _error(response)["details"]
    assert details == {"size": 2048, "limit": 1024}


# --- who may pull ---------------------------------------------------------
def test_a_viewer_may_not_pull(
    pull_client: TestClient,
    tenant: Tenant,
    source: StubSource,
    make_user: Callable[..., Any],
    session_factory: sessionmaker[Session],
) -> None:
    """Another organisation's contracts appear in the caller's portfolio, so this
    is a write — and the source is never asked, which is the part that matters."""
    make_user(tenant, email="viewer@acme.test", role=UserRole.VIEWER)
    token = _token(pull_client, tenant, "viewer@acme.test")
    source.document = [_record()]

    response = _pull(pull_client, token)

    assert response.status_code == 403, response.text
    assert source.urls == []
    assert _contracts(session_factory) == []


def test_a_pull_without_a_token_is_refused_before_the_source_is_asked(
    pull_client: TestClient,
    source: StubSource,
) -> None:
    source.document = [_record()]

    response = _pull(pull_client, None)

    assert response.status_code == 401, response.text
    assert source.urls == []
