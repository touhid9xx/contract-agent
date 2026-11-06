"""Account lifecycle and session revocation (gap 8).

Gap 6 gave a session a lifetime; this slice gives the *account* a way to end one
early, and a way to be administered without a database shell. Both halves exist
because a session that can only expire is a session an attacker keeps, and an
account that can only be created is an account an operator has to edit by hand.

Four routes, tested through the real ASGI app because every interesting property
here lives at a seam, not in a function:

* ``POST /auth/logout-all`` — ends every session including the caller's.
* ``POST /auth/password`` — proves the old password, then ends every *other*
  session and rotates the caller's.
* ``PATCH /auth/users/{id}`` — role and active state, admin-only, tenant-scoped.
* ``POST /auth/users/{id}/password-reset`` — a generated password, returned once.

The recurring question is the same one gap 6 asked, one level up: *what does an
attacker holding this credential get once the account has been revoked?* The
answer must be nothing, and it must be nothing on the **very next request** —
not in fifteen minutes when the access token happens to expire. That is why most
tests below assert against ``GET /auth/me`` with a token minted *before* the
revocation: it is the only way to see the difference between "the epoch was
bumped" and "the epoch was bumped but nothing reads it".
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.enums import UserRole
from app.db.models import AuditLog, RefreshToken, Tenant, User
from app.db.session import session_scope
from app.main import create_app

LOGIN_PATH = "/api/v1/auth/login"
REFRESH_PATH = "/api/v1/auth/refresh"
ME_PATH = "/api/v1/auth/me"
LOGOUT_ALL_PATH = "/api/v1/auth/logout-all"
PASSWORD_PATH = "/api/v1/auth/password"
USERS_PATH = "/api/v1/auth/users"

#: Matches ``conftest.DEFAULT_PASSWORD``; protects an in-memory database only.
PASSWORD = "correct-horse-battery-staple"  # nosec B105
NEW_PASSWORD = "another-perfectly-fine-passphrase"  # nosec B105

#: Every refusal below is an API contract, so each is spelled out rather than
#: imported: a reworded constant should fail a test, not silently agree with it.
UNAUTHENTICATED = "Not authenticated"
HIGHER_ROLE = "This operation requires a higher role"
WRONG_PASSWORD = "Current password is incorrect"
SAME_PASSWORD = "New password must differ from the current one"
NOTHING_TO_CHANGE = "Provide at least one of role or is_active"
NO_SUCH_USER = "No such user in this tenant"
ACCOUNT_INACTIVE = "Account is deactivated; reactivate it before resetting the password"
SELF_CHANGE_REFUSED = (
    "An administrator cannot change their own role or active state; ask a colleague"
)
TOO_MANY_PASSWORD_ATTEMPTS = "Too many incorrect password attempts; try again later"

#: Small enough that exhausting the budget costs three requests, not five.
MAX_ATTEMPTS = 3


# --- helpers --------------------------------------------------------------
def _login(client: TestClient, tenant_slug: str, email: str, password: str = PASSWORD) -> Response:
    return client.post(
        LOGIN_PATH, json={"tenant": tenant_slug, "email": email, "password": password}
    )


def _token(client: TestClient, tenant_slug: str, email: str, password: str = PASSWORD) -> str:
    """Log in and return the access token, asserting the login was admitted."""
    response = _login(client, tenant_slug, email, password)
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def _refresh_token(
    client: TestClient, tenant_slug: str, email: str, password: str = PASSWORD
) -> str:
    response = _login(client, tenant_slug, email, password)
    assert response.status_code == 200, response.text
    return str(response.json()["refresh_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _me(client: TestClient, token: str) -> Response:
    return client.get(ME_PATH, headers=_auth(token))


def _error(response: Response) -> dict[str, Any]:
    payload = response.json()
    assert "error" in payload, payload
    return dict(payload["error"])


def _message(response: Response) -> str:
    return str(_error(response)["message"])


def _change_password(
    client: TestClient, token: str, *, current: str = PASSWORD, new: str = NEW_PASSWORD
) -> Response:
    return client.post(
        PASSWORD_PATH,
        headers=_auth(token),
        json={"current_password": current, "new_password": new},
    )


def _patch_user(client: TestClient, token: str, user_id: uuid.UUID, **body: Any) -> Response:
    return client.patch(f"{USERS_PATH}/{user_id}", headers=_auth(token), json=body)


def _reset_password(client: TestClient, token: str, user_id: uuid.UUID) -> Response:
    return client.post(f"{USERS_PATH}/{user_id}/password-reset", headers=_auth(token))


def _load_user(session_factory: sessionmaker[Session], user_id: uuid.UUID) -> User:
    """Read one user row in its own transaction, fresh from the database."""
    with session_scope(session_factory) as session:
        row = session.get(User, user_id)
        assert row is not None, "fixture user vanished"
        return row


def _token_rows(session_factory: sessionmaker[Session], user_id: uuid.UUID) -> list[RefreshToken]:
    with session_scope(session_factory) as session:
        stmt = select(RefreshToken).where(RefreshToken.user_id == user_id)
        return list(session.execute(stmt).scalars())


def _audit_rows(session_factory: sessionmaker[Session], action: str) -> list[AuditLog]:
    with session_scope(session_factory) as session:
        stmt = select(AuditLog).where(AuditLog.action == action)
        return list(session.execute(stmt).scalars())


# --- fixtures -------------------------------------------------------------
@pytest.fixture
def make_tenant(session_factory: sessionmaker[Session]) -> Callable[..., Tenant]:
    """Create one committed tenant, so the running app's session can see it."""

    def _create(slug: str = "acme", name: str = "Acme Ltd") -> Tenant:
        with session_scope(session_factory) as session:
            tenant = Tenant(name=name, slug=slug)
            session.add(tenant)
            session.flush()
            return tenant

    return _create


@pytest.fixture
def tight_settings(settings: Settings) -> Settings:
    """The conftest settings with a password-attempt budget small enough to spend.

    ``model_copy`` rather than a fresh ``Settings(...)`` so the lowered Argon2 cost
    from ``conftest`` survives — this file hashes on nearly every test.
    """
    return settings.model_copy(update={"login_max_attempts": MAX_ATTEMPTS})


@pytest.fixture
def tight_client(
    tight_settings: Settings, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    app = create_app(tight_settings, session_factory=session_factory)
    with TestClient(app) as test_client:
        yield test_client


# --- POST /auth/logout-all ------------------------------------------------
def test_logout_all_kills_the_access_token_that_asked_for_it(
    client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    """The caller's own token must stop working immediately.

    An epoch bump is invisible unless something reads it, so this is asserted by
    making a request rather than by inspecting the column.
    """
    owner = make_tenant()
    user = make_user(owner, email="ops@acme.test")
    token = _token(client, owner.slug, user.email)
    assert _me(client, token).status_code == 200

    response = client.post(LOGOUT_ALL_PATH, headers=_auth(token))

    assert response.status_code == 204, response.text
    assert _me(client, token).status_code == 401


def test_logout_all_revokes_the_stored_refresh_rows(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """Both halves of "end a session" are needed: the row, not just the epoch.

    A stale-epoch row would be refused on redemption anyway, but leaving it live
    means the only thing standing between an attacker and a session is a check in
    another table.
    """
    owner = make_tenant()
    user = make_user(owner, email="ops@acme.test")
    token = _token(client, owner.slug, user.email)
    # A second session for the same account: logout-all must end both, not just
    # the caller's. The assertion below checks every row, so this call is only
    # here to create the second one.
    _refresh_token(client, owner.slug, user.email)

    assert client.post(LOGOUT_ALL_PATH, headers=_auth(token)).status_code == 204

    rows = _token_rows(session_factory, user.id)
    assert rows, "expected the login to have stored a refresh row"
    assert all(row.revoked_at is not None for row in rows)


def test_a_refresh_token_minted_before_logout_all_cannot_be_redeemed(
    client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    owner = make_tenant()
    user = make_user(owner, email="ops@acme.test")
    raw_refresh = _refresh_token(client, owner.slug, user.email)
    token = _token(client, owner.slug, user.email)

    assert client.post(LOGOUT_ALL_PATH, headers=_auth(token)).status_code == 204

    response = client.post(REFRESH_PATH, json={"refresh_token": raw_refresh})

    assert response.status_code == 401, response.text


def test_logout_all_leaves_a_colleagues_sessions_alone(
    client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    """Scoped to the caller. A "sign out everywhere" that signed everyone out
    would be a denial-of-service button on the tenant."""
    owner = make_tenant()
    caller = make_user(owner, email="ops@acme.test")
    colleague = make_user(owner, email="dev@acme.test")
    caller_token = _token(client, owner.slug, caller.email)
    colleague_token = _token(client, owner.slug, colleague.email)

    assert client.post(LOGOUT_ALL_PATH, headers=_auth(caller_token)).status_code == 204

    assert _me(client, colleague_token).status_code == 200


def test_logout_all_requires_authentication(client: TestClient) -> None:
    response = client.post(LOGOUT_ALL_PATH)

    assert response.status_code == 401
    assert _message(response) == UNAUTHENTICATED


# --- POST /auth/password --------------------------------------------------
def test_a_wrong_current_password_is_refused_and_changes_nothing(
    client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    owner = make_tenant()
    user = make_user(owner, email="ops@acme.test")
    token = _token(client, owner.slug, user.email)

    response = _change_password(client, token, current="not-the-password")

    assert response.status_code == 401
    assert _message(response) == WRONG_PASSWORD
    # The old password still works and the token still works: a refused write
    # must leave no trace at all.
    assert _login(client, owner.slug, user.email).status_code == 200
    assert _me(client, token).status_code == 200


def test_changing_the_password_ends_every_other_session(
    client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    """The whole point. A password changes because someone else may know it, so
    the sessions it opened must not outlive it."""
    owner = make_tenant()
    user = make_user(owner, email="ops@acme.test")
    other_device = _token(client, owner.slug, user.email)
    current_device = _token(client, owner.slug, user.email)

    response = _change_password(client, current_device)

    assert response.status_code == 200, response.text
    assert _me(client, other_device).status_code == 401


def test_changing_the_password_rotates_the_callers_own_session(
    client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    """Signing the caller out of the device they are sitting at would teach users
    to avoid the button, so the response is a new working pair."""
    owner = make_tenant()
    user = make_user(owner, email="ops@acme.test")
    token = _token(client, owner.slug, user.email)

    body = _change_password(client, token).json()

    assert set(body) >= {"access_token", "refresh_token", "refresh_expires_at"}
    assert _me(client, str(body["access_token"])).status_code == 200
    # ...and the token it replaced is dead.
    assert _me(client, token).status_code == 401


def test_the_rotated_refresh_token_redeems_and_the_previous_one_does_not(
    client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    """The successor is in a *new* family, so the tokens the change revoked can
    never be confused with it — and replaying one must not revoke it."""
    owner = make_tenant()
    user = make_user(owner, email="ops@acme.test")
    old_refresh = _refresh_token(client, owner.slug, user.email)
    token = _token(client, owner.slug, user.email)

    new_refresh = str(_change_password(client, token).json()["refresh_token"])

    assert client.post(REFRESH_PATH, json={"refresh_token": old_refresh}).status_code == 401
    rotated = client.post(REFRESH_PATH, json={"refresh_token": new_refresh})
    assert rotated.status_code == 200, rotated.text


def test_the_new_password_authenticates_and_the_old_one_does_not(
    client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    owner = make_tenant()
    user = make_user(owner, email="ops@acme.test")
    token = _token(client, owner.slug, user.email)

    assert _change_password(client, token).status_code == 200

    assert _login(client, owner.slug, user.email, NEW_PASSWORD).status_code == 200
    assert _login(client, owner.slug, user.email, PASSWORD).status_code == 401


def test_reusing_the_current_password_as_the_new_one_is_refused(
    client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    """Refused rather than accepted as a no-op: the change ends every other
    session, so "nothing changed" must not be reported as "you are signed out
    everywhere"."""
    owner = make_tenant()
    user = make_user(owner, email="ops@acme.test")
    token = _token(client, owner.slug, user.email)

    response = _change_password(client, token, current=PASSWORD, new=PASSWORD)

    assert response.status_code == 400
    assert _message(response) == SAME_PASSWORD


def test_a_password_change_is_refused_once_the_attempt_budget_is_spent(
    tight_client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """A stolen access token must not be a free oracle for the password behind
    it. The counter is shared with ``/login``, so this is the same arithmetic."""
    owner = make_tenant()
    user = make_user(owner, email="ops@acme.test")
    token = _token(tight_client, owner.slug, user.email)

    for _ in range(MAX_ATTEMPTS):
        assert _change_password(tight_client, token, current="wrong").status_code == 401

    response = _change_password(tight_client, token, current="wrong")

    assert response.status_code == 429, response.text
    assert _message(response) == TOO_MANY_PASSWORD_ATTEMPTS
    assert int(response.headers["Retry-After"]) >= 1


def test_a_successful_change_clears_the_failed_attempt_counter(
    tight_client: TestClient,
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """Otherwise a user who mistyped twice is one attempt from a lockout they
    never earned — and would discover it at the login form, not here."""
    owner = make_tenant()
    user = make_user(owner, email="ops@acme.test")

    for _ in range(MAX_ATTEMPTS - 1):
        token = _token(tight_client, owner.slug, user.email)
        assert _change_password(tight_client, token, current="wrong").status_code == 401
    token = _token(tight_client, owner.slug, user.email)
    assert _change_password(tight_client, token).status_code == 200

    # A fresh wrong login is now 401 (counted from zero), not 429.
    assert _login(tight_client, owner.slug, user.email, "wrong").status_code == 401


def test_a_password_change_leaves_an_audit_row(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """The interesting fact is not who did it — actor and subject are the same
    person — but that every other session stopped working at that instant."""
    owner = make_tenant()
    user = make_user(owner, email="ops@acme.test")
    token = _token(client, owner.slug, user.email)

    assert _change_password(client, token).status_code == 200

    rows = _audit_rows(session_factory, "user.password_changed")
    assert len(rows) == 1
    assert rows[0].entity_id == str(user.id)
    assert rows[0].actor_id == user.id
    assert rows[0].entity == "users"


def test_the_audit_row_records_the_epoch_bump_and_no_credential(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """``token_epoch`` is the observable consequence, so it belongs in the
    snapshot. The hash is everything an attacker wants and an auditor may not
    see, so it must be absent by construction."""
    owner = make_tenant()
    user = make_user(owner, email="ops@acme.test")
    token = _token(client, owner.slug, user.email)

    assert _change_password(client, token).status_code == 200

    row = _audit_rows(session_factory, "user.password_changed")[0]
    before = json.loads(row.before_state or "{}")
    after = json.loads(row.after_state or "{}")
    assert after["token_epoch"] == before["token_epoch"] + 1
    for snapshot in (before, after):
        assert "password_hash" not in snapshot
        assert NEW_PASSWORD not in json.dumps(snapshot)
        assert PASSWORD not in json.dumps(snapshot)


def test_changing_the_password_requires_authentication(client: TestClient) -> None:
    response = client.post(
        PASSWORD_PATH, json={"current_password": PASSWORD, "new_password": NEW_PASSWORD}
    )

    assert response.status_code == 401
    assert _message(response) == UNAUTHENTICATED


def test_a_short_new_password_is_rejected_before_anything_is_written(
    client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    """A change must not be able to set a password creation would have refused."""
    owner = make_tenant()
    user = make_user(owner, email="ops@acme.test")
    token = _token(client, owner.slug, user.email)

    response = _change_password(client, token, new="short")

    assert response.status_code == 422, response.text
    assert _login(client, owner.slug, user.email).status_code == 200


# --- PATCH /auth/users/{id} ----------------------------------------------
def test_deactivating_a_colleague_ends_their_session_at_once(
    client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    owner = make_tenant()
    admin = make_user(owner, email="admin@acme.test")
    target = make_user(owner, email="dev@acme.test")
    admin_token = _token(client, owner.slug, admin.email)
    target_token = _token(client, owner.slug, target.email)

    response = _patch_user(client, admin_token, target.id, is_active=False)

    assert response.status_code == 200, response.text
    assert response.json()["role"] == target.role.value
    # The response reports the change: a body identical to a reactivation would
    # leave the caller inferring the outcome from a 200 either act returns.
    assert response.json()["is_active"] is False
    assert _me(client, target_token).status_code == 401


def test_a_demotion_takes_effect_on_the_next_request(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """The role travels *inside* the access token, so without an epoch bump a
    demoted administrator keeps every permission for the rest of its life."""
    owner = make_tenant()
    admin = make_user(owner, email="admin@acme.test")
    colleague = make_user(owner, email="boss@acme.test", role=UserRole.ADMIN)
    admin_token = _token(client, owner.slug, admin.email)
    colleague_token = _token(client, owner.slug, colleague.email)
    assert client.get(USERS_PATH, headers=_auth(colleague_token)).status_code == 200

    response = _patch_user(client, admin_token, colleague.id, role=UserRole.VIEWER.value)

    assert response.status_code == 200, response.text
    assert _load_user(session_factory, colleague.id).role == UserRole.VIEWER
    assert _me(client, colleague_token).status_code == 401


def test_reactivating_does_not_resurrect_a_token_minted_before_deactivation(
    client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    """The epoch only moves forward. A deactivate-then-reactivate cycle must not
    hand back the session the deactivation was meant to end."""
    owner = make_tenant()
    admin = make_user(owner, email="admin@acme.test")
    target = make_user(owner, email="dev@acme.test")
    admin_token = _token(client, owner.slug, admin.email)
    target_token = _token(client, owner.slug, target.email)

    assert _patch_user(client, admin_token, target.id, is_active=False).status_code == 200
    assert _patch_user(client, admin_token, target.id, is_active=True).status_code == 200

    assert _me(client, target_token).status_code == 401
    # A fresh login still works: reactivation is real, not a locked door.
    assert _login(client, owner.slug, target.email).status_code == 200


def test_a_deactivation_revokes_the_stored_refresh_rows(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    owner = make_tenant()
    admin = make_user(owner, email="admin@acme.test")
    target = make_user(owner, email="dev@acme.test")
    admin_token = _token(client, owner.slug, admin.email)
    target_token = _token(client, owner.slug, target.email)

    assert _patch_user(client, admin_token, target.id, is_active=False).status_code == 200

    rows = _token_rows(session_factory, target.id)
    assert rows
    assert all(row.revoked_at is not None for row in rows)
    assert _me(client, target_token).status_code == 401


def test_an_administrator_cannot_change_their_own_account(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """Self-deactivation is how a tenant loses its only administrator, and there
    is no recovery path: ``create-admin`` refuses a slug that already exists."""
    owner = make_tenant()
    admin = make_user(owner, email="admin@acme.test")
    admin_token = _token(client, owner.slug, admin.email)

    response = _patch_user(client, admin_token, admin.id, is_active=False)

    assert response.status_code == 409
    assert _message(response) == SELF_CHANGE_REFUSED
    assert _load_user(session_factory, admin.id).is_active is True


def test_a_request_carrying_neither_field_is_refused(
    client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    """A no-op answered with 200 would hide a client bug at the call site."""
    owner = make_tenant()
    admin = make_user(owner, email="admin@acme.test")
    target = make_user(owner, email="dev@acme.test")
    admin_token = _token(client, owner.slug, admin.email)

    response = _patch_user(client, admin_token, target.id)

    assert response.status_code == 400
    assert _message(response) == NOTHING_TO_CHANGE


def test_a_patch_that_changes_nothing_does_not_end_the_targets_sessions(
    client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    """Idempotent by value, not just by status: re-sending the current role must
    not sign a colleague out as a side effect of a no-op."""
    owner = make_tenant()
    admin = make_user(owner, email="admin@acme.test")
    target = make_user(owner, email="dev@acme.test")
    admin_token = _token(client, owner.slug, admin.email)
    target_token = _token(client, owner.slug, target.email)

    response = _patch_user(client, admin_token, target.id, role=target.role.value)

    assert response.status_code == 200, response.text
    assert _me(client, target_token).status_code == 200


def test_an_unknown_user_id_is_a_404(
    client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    owner = make_tenant()
    admin = make_user(owner, email="admin@acme.test")
    admin_token = _token(client, owner.slug, admin.email)

    response = _patch_user(client, admin_token, uuid.uuid4(), is_active=False)

    assert response.status_code == 404
    assert _message(response) == NO_SUCH_USER


def test_another_tenants_user_is_a_404_not_a_write(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """The IDOR/BOLA case. The repository is built from the principal's tenant,
    so the id resolves to ``None`` — same answer as one that never existed,
    which is what stops the route from being a directory probe."""
    ours = make_tenant()
    theirs = make_tenant(slug="other", name="Other Ltd")
    admin = make_user(ours, email="admin@acme.test")
    stranger = make_user(theirs, email="dev@other.test")
    admin_token = _token(client, ours.slug, admin.email)

    response = _patch_user(client, admin_token, stranger.id, is_active=False)

    assert response.status_code == 404
    assert _message(response) == NO_SUCH_USER
    assert _load_user(session_factory, stranger.id).is_active is True


def test_a_non_admin_cannot_reach_the_admin_routes(
    client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    owner = make_tenant()
    viewer = make_user(owner, email="viewer@acme.test", role=UserRole.VIEWER)
    target = make_user(owner, email="dev@acme.test")
    viewer_token = _token(client, owner.slug, viewer.email)

    response = _patch_user(client, viewer_token, target.id, is_active=False)

    assert response.status_code == 403
    assert _message(response) == HIGHER_ROLE


def test_an_admin_change_leaves_an_audit_row_naming_the_field_that_moved(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    owner = make_tenant()
    admin = make_user(owner, email="admin@acme.test")
    target = make_user(owner, email="dev@acme.test", role=UserRole.VIEWER)
    admin_token = _token(client, owner.slug, admin.email)

    assert _patch_user(client, admin_token, target.id, role=UserRole.ADMIN.value).status_code == 200

    rows = _audit_rows(session_factory, "user.updated")
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_id == admin.id
    assert row.entity_id == str(target.id)
    assert json.loads(row.before_state or "{}")["role"] == UserRole.VIEWER.value
    assert json.loads(row.after_state or "{}")["role"] == UserRole.ADMIN.value


# --- POST /auth/users/{id}/password-reset --------------------------------
def test_a_reset_returns_a_password_that_actually_works(
    client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    owner = make_tenant()
    admin = make_user(owner, email="admin@acme.test")
    target = make_user(owner, email="dev@acme.test")
    admin_token = _token(client, owner.slug, admin.email)

    response = _reset_password(client, admin_token, target.id)

    assert response.status_code == 200, response.text
    temporary = str(response.json()["temporary_password"])
    assert temporary
    assert _login(client, owner.slug, target.email, temporary).status_code == 200
    assert _login(client, owner.slug, target.email, PASSWORD).status_code == 401


def test_a_reset_ends_the_targets_sessions(
    client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    """The reset exists because the account may be compromised; leaving the
    attacker's session alive would defeat it."""
    owner = make_tenant()
    admin = make_user(owner, email="admin@acme.test")
    target = make_user(owner, email="dev@acme.test")
    admin_token = _token(client, owner.slug, admin.email)
    target_token = _token(client, owner.slug, target.email)

    assert _reset_password(client, admin_token, target.id).status_code == 200

    assert _me(client, target_token).status_code == 401


def test_an_administrator_cannot_reset_their_own_password_this_way(
    client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    """``/auth/password`` is the path that proves the current password. A
    self-reset here would skip that proof entirely."""
    owner = make_tenant()
    admin = make_user(owner, email="admin@acme.test")
    admin_token = _token(client, owner.slug, admin.email)

    response = _reset_password(client, admin_token, admin.id)

    assert response.status_code == 409
    assert _message(response) == SELF_CHANGE_REFUSED


def test_a_reset_of_a_deactivated_account_is_refused(
    client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    """Returning a password login is configured to refuse would be a convincing
    lie. Reactivate first, so the two acts stay separately auditable."""
    owner = make_tenant()
    admin = make_user(owner, email="admin@acme.test")
    target = make_user(owner, email="dev@acme.test", is_active=False)
    admin_token = _token(client, owner.slug, admin.email)

    response = _reset_password(client, admin_token, target.id)

    assert response.status_code == 409
    assert _message(response) == ACCOUNT_INACTIVE


def test_a_reset_of_another_tenants_user_is_a_404(
    client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    ours = make_tenant()
    theirs = make_tenant(slug="other", name="Other Ltd")
    admin = make_user(ours, email="admin@acme.test")
    stranger = make_user(theirs, email="dev@other.test")
    admin_token = _token(client, ours.slug, admin.email)

    response = _reset_password(client, admin_token, stranger.id)

    assert response.status_code == 404
    assert _message(response) == NO_SUCH_USER


def test_a_viewer_cannot_reset_anyones_password(
    client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    owner = make_tenant()
    viewer = make_user(owner, email="viewer@acme.test", role=UserRole.VIEWER)
    target = make_user(owner, email="dev@acme.test")
    viewer_token = _token(client, owner.slug, viewer.email)

    response = _reset_password(client, viewer_token, target.id)

    assert response.status_code == 403
    assert _message(response) == HIGHER_ROLE


def test_the_temporary_password_never_reaches_the_audit_log(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """The row says a reset happened and who ordered it. The value exists only in
    the response that carried it — anything else makes the audit log a credential
    store readable by every operator."""
    owner = make_tenant()
    admin = make_user(owner, email="admin@acme.test")
    target = make_user(owner, email="dev@acme.test")
    admin_token = _token(client, owner.slug, admin.email)

    temporary = str(_reset_password(client, admin_token, target.id).json()["temporary_password"])

    rows = _audit_rows(session_factory, "user.password_reset")
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_id == admin.id
    serialised = json.dumps([row.before_state, row.after_state, row.reason, row.correlation_id])
    assert temporary not in serialised
    assert "password_hash" not in serialised


def test_the_audit_row_reports_the_epoch_bump_a_reset_caused(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    owner = make_tenant()
    admin = make_user(owner, email="admin@acme.test")
    target = make_user(owner, email="dev@acme.test")
    admin_token = _token(client, owner.slug, admin.email)

    assert _reset_password(client, admin_token, target.id).status_code == 200

    row = _audit_rows(session_factory, "user.password_reset")[0]
    before = json.loads(row.before_state or "{}")
    after = json.loads(row.after_state or "{}")
    assert after["token_epoch"] == before["token_epoch"] + 1
