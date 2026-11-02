"""Reading a contract's extraction state (F7).

The route is the only place extraction output becomes visible, so the guarantees
that matter are: it is tenant-scoped, it never reveals a stored value, and a field
that blocks the pipeline is counted rather than hidden.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.core.crypto import FieldCipher
from app.db.enums import FieldName, FieldStatus, UserRole
from app.db.models import Contract, ExtractedField, Tenant
from app.db.session import session_scope

LOGIN_PATH = "/api/v1/auth/login"
FIELDS_PATH = "/api/v1/contracts/{contract_id}/fields"

#: Matches ``conftest.DEFAULT_PASSWORD``; protects in-memory databases only.
PASSWORD = "correct-horse-battery-staple"  # nosec B105
PLAINTEXT = "a.rahman@example.test"


@pytest.fixture
def cipher() -> FieldCipher:
    return FieldCipher(Fernet.generate_key().decode("ascii"))


@pytest.fixture
def contract(session_factory: sessionmaker[Session], tenant: Tenant) -> Contract:
    with session_scope(session_factory) as session:
        row = Contract(tenant_id=tenant.id, customer_ref="A. Rahman", end_date=date(2026, 1, 1))
        session.add(row)
        session.flush()
        return row


def add_field(
    session_factory: sessionmaker[Session],
    contract: Contract,
    cipher: FieldCipher,
    *,
    field_name: FieldName = FieldName.EMAIL,
    value: str = PLAINTEXT,
    confidence: float = 0.95,
    status: FieldStatus = FieldStatus.AUTO_ACCEPTED,
) -> None:
    with session_scope(session_factory) as session:
        session.add(
            ExtractedField(
                tenant_id=contract.tenant_id,
                contract_id=contract.id,
                field_name=field_name,
                raw_value=cipher.encrypt(value),
                normalized_value=value,
                confidence=confidence,
                status=status,
            )
        )


def auth(
    client: TestClient,
    tenant: Tenant,
    make_user: Callable[..., Any],
    *,
    role: UserRole = UserRole.OPERATOR,
    email: str = "ops@acme.test",
) -> dict[str, str]:
    make_user(tenant, email=email, role=role)
    response = client.post(
        LOGIN_PATH,
        json={"tenant": tenant.slug, "email": email, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


# --- the happy path -------------------------------------------------------
def test_a_contract_with_no_extracted_fields_is_an_empty_list(
    client: TestClient,
    contract: Contract,
    make_user: Callable[..., Any],
    tenant: Tenant,
) -> None:
    headers = auth(client, tenant, make_user)

    response = client.get(FIELDS_PATH.format(contract_id=contract.id), headers=headers)

    assert response.status_code == 200, response.text
    assert response.json() == {"contract_id": contract.id, "fields": [], "blocking": 0}


def test_each_field_reports_its_review_state(
    client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., Any],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract, cipher)
    headers = auth(client, tenant, make_user)

    body = client.get(FIELDS_PATH.format(contract_id=contract.id), headers=headers).json()

    assert body["fields"] == [
        {
            "field_name": "email",
            "status": "auto_accepted",
            "confidence": 0.95,
            "version": 1,
            "source_page": None,
            "source_line": None,
            "reviewed_at": None,
            "review_reason": None,
            "blocks_pipeline": False,
        }
    ]


def test_a_field_awaiting_review_is_counted_as_blocking(
    client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., Any],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract, cipher, confidence=0.55, status=FieldStatus.PENDING_REVIEW)
    add_field(
        session_factory,
        contract,
        cipher,
        field_name=FieldName.PHONE,
        value="+442079460958",
        status=FieldStatus.AUTO_ACCEPTED,
    )
    headers = auth(client, tenant, make_user)

    body = client.get(FIELDS_PATH.format(contract_id=contract.id), headers=headers).json()

    # The number an operator acts on: two fields, one of them a hard stop.
    assert body["blocking"] == 1
    assert [field["field_name"] for field in body["fields"]] == ["email", "phone"]


def test_fields_are_listed_in_a_stable_order(
    client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., Any],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract, cipher, field_name=FieldName.PHONE, value="+442079460958")
    add_field(session_factory, contract, cipher, field_name=FieldName.EMAIL)
    headers = auth(client, tenant, make_user)

    body = client.get(FIELDS_PATH.format(contract_id=contract.id), headers=headers).json()

    assert [field["field_name"] for field in body["fields"]] == ["email", "phone"]


# --- what must not leak ---------------------------------------------------
def test_the_stored_value_is_not_returned(
    client: TestClient,
    contract: Contract,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., Any],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract, cipher)
    headers = auth(client, tenant, make_user)

    body = client.get(FIELDS_PATH.format(contract_id=contract.id), headers=headers).text

    # Neither the plaintext nor the ciphertext: revealing a value is a review
    # detail action that has to be audited (D7-05).
    assert PLAINTEXT not in body
    assert cipher.encrypt(PLAINTEXT) not in body


def test_a_contract_of_another_tenant_is_not_found(
    client: TestClient,
    cipher: FieldCipher,
    session_factory: sessionmaker[Session],
    other_tenant: Tenant,
    make_user: Callable[..., Any],
    tenant: Tenant,
) -> None:
    with session_scope(session_factory) as session:
        foreign = Contract(tenant_id=other_tenant.id, customer_ref="Globex")
        session.add(foreign)
        session.flush()
        foreign_id = foreign.id
    headers = auth(client, tenant, make_user)

    response = client.get(FIELDS_PATH.format(contract_id=foreign_id), headers=headers)

    # 404 rather than 403: a 403 would confirm the id exists somewhere.
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_an_unknown_contract_is_not_found(
    client: TestClient,
    make_user: Callable[..., Any],
    tenant: Tenant,
) -> None:
    headers = auth(client, tenant, make_user)

    response = client.get(FIELDS_PATH.format(contract_id=987654), headers=headers)

    assert response.status_code == 404


# --- who may read ---------------------------------------------------------
def test_a_viewer_may_read_the_extraction_state(
    client: TestClient,
    contract: Contract,
    make_user: Callable[..., Any],
    tenant: Tenant,
) -> None:
    headers = auth(client, tenant, make_user, role=UserRole.VIEWER, email="viewer@acme.test")

    response = client.get(FIELDS_PATH.format(contract_id=contract.id), headers=headers)

    assert response.status_code == 200, response.text


def test_reading_requires_a_token(client: TestClient, contract: Contract) -> None:
    response = client.get(FIELDS_PATH.format(contract_id=contract.id))

    assert response.status_code == 401
