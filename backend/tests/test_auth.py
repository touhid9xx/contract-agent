"""End-to-end tests for authentication and tenant binding (F3).

These go through the real ASGI app against a real (in-memory) database, because
the guarantees under test are not local to any one function:

* login must not reveal *which* half of the credentials was wrong;
* a token must stop working the moment the row behind it changes, without a
  denylist and without waiting for ``exp``;
* ``tenant_id`` must come from the principal, so no request field can aim a read
  or a write at another organisation.

A test that exercised the router function directly would prove none of that.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import JWT_ALGORITHM, TOKEN_TYPE_ACCESS
from app.db.enums import UserRole
from app.db.models import Tenant, User
from app.db.session import session_scope

LOGIN_PATH = "/api/v1/auth/login"
ME_PATH = "/api/v1/auth/me"
USERS_PATH = "/api/v1/auth/users"

#: Matches ``conftest.DEFAULT_PASSWORD``. Not a credential: it protects
#: in-memory databases that only exist for the duration of a test.
PASSWORD = "correct-horse-battery-staple"  # nosec B105

#: The one sentence login returns for every rejection. Spelled out rather than
#: imported from the router: it is an API contract, so a change to the wording
#: should fail a test instead of being absorbed by a shared constant.
INVALID_CREDENTIALS = "Invalid tenant, email or password"


# --- helpers --------------------------------------------------------------
def _login(
    client: TestClient,
    tenant_slug: str,
    email: str,
    password: str = PASSWORD,
) -> Response:
    return client.post(
        LOGIN_PATH,
        json={"tenant": tenant_slug, "email": email, "password": password},
    )


def _token(
    client: TestClient,
    tenant: Tenant,
    user: User,
    password: str = PASSWORD,
) -> str:
    response = _login(client, tenant.slug, user.email, password)
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _error(response: Response) -> dict[str, Any]:
    """The error envelope; every failure in this app uses it."""
    payload = response.json()
    assert "error" in payload, payload
    return dict(payload["error"])


def _update_user(session_factory: sessionmaker[Session], user_id: uuid.UUID, **fields: Any) -> None:
    """Mutate one user row in its own committed transaction.

    Deliberately not the ``db_session`` fixture: the request under test runs in a
    different session, so a value that has not been committed is a value the app
    cannot see.
    """
    with session_scope(session_factory) as session:
        row = session.get(User, user_id)
        assert row is not None, "fixture user vanished"
        for key, value in fields.items():
            setattr(row, key, value)


def _set_tenant_active(
    session_factory: sessionmaker[Session], tenant_id: uuid.UUID, is_active: bool
) -> None:
    with session_scope(session_factory) as session:
        row = session.get(Tenant, tenant_id)
        assert row is not None, "fixture tenant vanished"
        row.is_active = is_active


def _delete_user(session_factory: sessionmaker[Session], user_id: uuid.UUID) -> None:
    with session_scope(session_factory) as session:
        row = session.get(User, user_id)
        assert row is not None, "fixture user vanished"
        session.delete(row)


@pytest.fixture
def make_tenant(session_factory: sessionmaker[Session]) -> Callable[..., Tenant]:
    """Create one committed tenant.

    The ``tenant``/``other_tenant`` fixtures live in ``conftest`` but are built on
    the shared ``db_session``; these tests mutate rows after the app has already
    read them, so they get their own committed writes instead.
    """

    def _create(slug: str = "acme", name: str = "Acme Ltd", is_active: bool = True) -> Tenant:
        with session_scope(session_factory) as session:
            owner = Tenant(name=name, slug=slug, is_active=is_active)
            session.add(owner)
            session.flush()
        return owner

    return _create


# --- login ----------------------------------------------------------------
def test_login_returns_a_bearer_token_and_the_users_details(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    acme = make_tenant()
    user = make_user(acme, email="ops@acme.test", role=UserRole.ADMIN)

    response = _login(client, "acme", "ops@acme.test")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["expires_in"] > 0
    assert body["user"] == {
        "id": str(user.id),
        "email": "ops@acme.test",
        "role": "admin",
        "tenant_id": str(acme.id),
        "tenant_slug": "acme",
        "is_active": True,
    }


def test_login_response_never_contains_the_stored_hash(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    acme = make_tenant()
    user = make_user(acme, email="ops@acme.test")

    body = _login(client, "acme", "ops@acme.test").text

    assert user.password_hash not in body
    assert PASSWORD not in body
    assert "password" not in body.lower()


def test_login_accepts_the_address_in_any_case_and_with_padding(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """Storage and lookup both normalise, so case cannot fork an account."""
    acme = make_tenant()
    make_user(acme, email="ops@acme.test")

    response = _login(client, "acme", "  Ops@Acme.Test  ")

    assert response.status_code == 200, response.text
    assert response.json()["user"]["email"] == "ops@acme.test"


def test_login_accepts_the_tenant_slug_in_any_case(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    acme = make_tenant(slug="acme")
    make_user(acme, email="ops@acme.test")

    response = _login(client, "  ACME  ", "ops@acme.test")

    assert response.status_code == 200, response.text
    assert response.json()["user"]["tenant_slug"] == "acme"


def test_every_login_failure_looks_identical(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """Unknown tenant, unknown address, wrong password and a disabled account must
    be indistinguishable — including the wording, or login becomes an oracle for
    which addresses and organisations exist."""
    acme = make_tenant()
    make_user(acme, email="ops@acme.test")
    make_user(acme, email="gone@acme.test", is_active=False)

    responses = [
        _login(client, "no-such-tenant", "ops@acme.test"),
        _login(client, "acme", "nobody@acme.test"),
        _login(client, "acme", "ops@acme.test", "wrong-password-entirely"),
        _login(client, "acme", "gone@acme.test"),
    ]

    for response in responses:
        assert response.status_code == 401, response.text

    errors = [_error(response) for response in responses]
    assert {error["message"] for error in errors} == {INVALID_CREDENTIALS}
    assert {error["code"] for error in errors} == {"unauthorized"}
    # The message names no account, no tenant and no hash: it is the same fixed
    # sentence whether the organisation, the address or the password was wrong.
    for error in errors:
        assert "ops@acme.test" not in error["message"]
        assert "gone@acme.test" not in error["message"]
        assert "acme" not in error["message"].lower()


def test_an_inactive_tenant_cannot_log_in(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    suspended = make_tenant(slug="acme", is_active=False)
    make_user(suspended, email="ops@acme.test")

    response = _login(client, "acme", "ops@acme.test")

    assert response.status_code == 401
    assert _error(response)["code"] == "unauthorized"


def test_a_user_of_another_tenant_cannot_be_matched_by_slug(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """The slug selects the organisation to check against; an address that only
    exists in a different one must not authenticate."""
    acme = make_tenant(slug="acme")
    globex = make_tenant(slug="globex", name="Globex Ltd")
    make_user(acme, email="ops@acme.test")

    assert _login(client, "globex", "ops@acme.test").status_code == 401

    # ... and the same address resolves to the tenant the row actually lives in.
    response = _login(client, acme.slug, "ops@acme.test")
    assert response.json()["user"]["tenant_id"] == str(acme.id)
    assert globex.id != acme.id


# --- /auth/me -------------------------------------------------------------
def test_me_rejects_a_missing_token_with_a_bearer_challenge(client: TestClient) -> None:
    """401 not 403, and with ``WWW-Authenticate``: the caller is unauthenticated,
    not insufficiently privileged."""
    response = client.get(ME_PATH)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert _error(response)["code"] == "unauthorized"


@pytest.mark.parametrize(
    "header",
    [
        "Bearer",
        "Bearer ",
        "not-even-a-scheme",
        "Basic b3BzOm9wcw==",
    ],
)
def test_me_rejects_a_header_it_cannot_parse(client: TestClient, header: str) -> None:
    response = client.get(ME_PATH, headers={"Authorization": header})

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_me_rejects_a_garbage_token(client: TestClient, settings: Any) -> None:
    response = client.get(ME_PATH, headers=_auth("not.a.jwt"))

    assert response.status_code == 401
    assert settings.secret_key not in response.text


def test_me_returns_the_resolved_principal(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    acme = make_tenant()
    user = make_user(acme, email="ops@acme.test", role=UserRole.OPERATOR)
    token = _token(client, acme, user)

    response = client.get(ME_PATH, headers=_auth(token))

    assert response.status_code == 200, response.text
    assert response.json() == {
        "id": str(user.id),
        "email": "ops@acme.test",
        "role": "operator",
        "tenant_id": str(acme.id),
        "tenant_slug": "acme",
        "is_active": True,
    }


# --- revocation: the row wins, not the token ------------------------------
def test_bumping_the_token_epoch_invalidates_an_unexpired_token(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
    session_factory: sessionmaker[Session],
) -> None:
    """Logout-everywhere / password change: the token is still signed and still
    inside its lifetime, and must nevertheless stop working immediately."""
    acme = make_tenant()
    user = make_user(acme, email="ops@acme.test")
    token = _token(client, acme, user)
    assert client.get(ME_PATH, headers=_auth(token)).status_code == 200

    _update_user(session_factory, user.id, token_epoch=1)

    assert client.get(ME_PATH, headers=_auth(token)).status_code == 401


def test_deactivating_the_user_invalidates_an_unexpired_token(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
    session_factory: sessionmaker[Session],
) -> None:
    acme = make_tenant()
    user = make_user(acme, email="ops@acme.test")
    token = _token(client, acme, user)

    _update_user(session_factory, user.id, is_active=False)

    assert client.get(ME_PATH, headers=_auth(token)).status_code == 401


def test_deactivating_the_tenant_invalidates_an_unexpired_token(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
    session_factory: sessionmaker[Session],
) -> None:
    """Suspending an organisation must not leave its operators working."""
    acme = make_tenant()
    user = make_user(acme, email="ops@acme.test")
    token = _token(client, acme, user)

    _set_tenant_active(session_factory, acme.id, False)

    assert client.get(ME_PATH, headers=_auth(token)).status_code == 401


def test_a_deleted_users_token_stops_working(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
    session_factory: sessionmaker[Session],
) -> None:
    acme = make_tenant()
    user = make_user(acme, email="ops@acme.test")
    token = _token(client, acme, user)

    _delete_user(session_factory, user.id)

    assert client.get(ME_PATH, headers=_auth(token)).status_code == 401


def test_a_token_whose_tenant_claim_contradicts_the_row_is_rejected(
    client: TestClient,
    settings: Any,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """A correctly signed token is not a capability. If its ``tid`` disagrees with
    the user row, the request is refused rather than resolved to either side."""
    acme = make_tenant(slug="acme")
    globex = make_tenant(slug="globex", name="Globex Ltd")
    user = make_user(acme, email="ops@acme.test")

    now = datetime.now(UTC)
    forged = jwt.encode(
        {
            "sub": str(user.id),
            "tid": str(globex.id),
            "role": UserRole.ADMIN.value,
            "epoch": 0,
            "typ": TOKEN_TYPE_ACCESS,
            "iss": settings.app_name,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
        },
        settings.secret_key,
        algorithm=JWT_ALGORITHM,
    )

    assert client.get(ME_PATH, headers=_auth(forged)).status_code == 401


# --- user administration --------------------------------------------------
def test_listing_users_returns_only_colleagues(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    acme = make_tenant(slug="acme")
    globex = make_tenant(slug="globex", name="Globex Ltd")
    admin = make_user(acme, email="admin@acme.test", role=UserRole.ADMIN)
    make_user(acme, email="viewer@acme.test", role=UserRole.VIEWER)
    make_user(globex, email="admin@globex.test", role=UserRole.ADMIN)

    response = client.get(USERS_PATH, headers=_auth(_token(client, acme, admin)))

    assert response.status_code == 200, response.text
    emails = {row["email"] for row in response.json()}
    assert emails == {"admin@acme.test", "viewer@acme.test"}
    assert all(row["tenant_id"] == str(acme.id) for row in response.json())


def test_creating_a_user_stamps_the_callers_tenant(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """There is no tenant field in the body, so a write cannot be aimed at
    another organisation."""
    acme = make_tenant(slug="acme")
    globex = make_tenant(slug="globex", name="Globex Ltd")
    admin = make_user(acme, email="admin@acme.test", role=UserRole.ADMIN)

    response = client.post(
        USERS_PATH,
        json={"email": "new@acme.test", "password": "a-long-enough-password"},
        headers=_auth(_token(client, acme, admin)),
    )

    assert response.status_code == 201, response.text
    created = response.json()
    assert created["tenant_id"] == str(acme.id)
    assert created["role"] == "viewer"
    assert globex.id != acme.id

    # The new account is real: it authenticates, and only against its own tenant.
    assert _login(client, "acme", "new@acme.test", "a-long-enough-password").status_code == 200
    assert _login(client, "globex", "new@acme.test", "a-long-enough-password").status_code == 401


def test_the_same_email_may_exist_in_two_tenants(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """Unique per tenant, not globally — a global constraint would leak that an
    address exists elsewhere."""
    acme = make_tenant(slug="acme")
    globex = make_tenant(slug="globex", name="Globex Ltd")
    acme_admin = make_user(acme, email="admin@acme.test", role=UserRole.ADMIN)
    globex_admin = make_user(globex, email="admin@globex.test", role=UserRole.ADMIN)

    body = {"email": "shared@example.test", "password": "a-long-enough-password"}
    first = client.post(USERS_PATH, json=body, headers=_auth(_token(client, acme, acme_admin)))
    second = client.post(USERS_PATH, json=body, headers=_auth(_token(client, globex, globex_admin)))

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["tenant_id"] == str(acme.id)
    assert second.json()["tenant_id"] == str(globex.id)

    # The same credentials resolve to different organisations by slug.
    assert _login(client, "acme", "shared@example.test", body["password"]).json()["user"][
        "tenant_id"
    ] == str(acme.id)
    assert _login(client, "globex", "shared@example.test", body["password"]).json()["user"][
        "tenant_id"
    ] == str(globex.id)


def test_creating_a_duplicate_email_in_the_same_tenant_conflicts(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    acme = make_tenant()
    admin = make_user(acme, email="admin@acme.test", role=UserRole.ADMIN)
    token = _token(client, acme, admin)
    body = {"email": "new@acme.test", "password": "a-long-enough-password"}

    assert client.post(USERS_PATH, json=body, headers=_auth(token)).status_code == 201
    response = client.post(USERS_PATH, json=body, headers=_auth(token))

    assert response.status_code == 409
    assert _error(response)["code"] == "conflict"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("email", "not-an-email"),
        ("email", "@acme.test"),
        ("email", "ops@"),
        ("password", "short"),
    ],
)
def test_creating_a_user_validates_its_input(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
    field: str,
    value: str,
) -> None:
    acme = make_tenant()
    admin = make_user(acme, email="admin@acme.test", role=UserRole.ADMIN)
    body = {"email": "new@acme.test", "password": "a-long-enough-password"}
    body[field] = value

    response = client.post(USERS_PATH, json=body, headers=_auth(_token(client, acme, admin)))

    assert response.status_code == 422
    assert _error(response)["code"] == "validation_error"


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER])
@pytest.mark.parametrize("method", ["get", "post"])
def test_a_non_admin_cannot_manage_users(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
    role: UserRole,
    method: str,
) -> None:
    """Authenticated but insufficiently privileged is 403, distinctly from 401."""
    acme = make_tenant()
    user = make_user(acme, email="member@acme.test", role=role)
    headers = _auth(_token(client, acme, user))

    response = (
        client.get(USERS_PATH, headers=headers)
        if method == "get"
        else client.post(
            USERS_PATH,
            json={"email": "new@acme.test", "password": "a-long-enough-password"},
            headers=headers,
        )
    )

    assert response.status_code == 403, response.text
    assert _error(response)["code"] == "forbidden"


def test_an_admin_route_still_requires_a_token(client: TestClient) -> None:
    assert client.get(USERS_PATH).status_code == 401
