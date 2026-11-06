"""Refresh tokens (gap 6): the policy, the stored digest, and session rotation.

Three layers, for the reason ``test_login_throttle`` has three — each fails in a
way the others cannot see:

* ``mint_refresh_token`` / ``refresh_token_hash`` / ``refresh_expiry`` — pure
  functions, so entropy, digest shape and deadline arithmetic need no database.
* ``RefreshTokenRepository`` — the conditional spend. Rotation's whole safety
  argument rests on one ``UPDATE ... WHERE used_at IS NULL``: a read-then-write
  would let two requests both mint a successor from one token, which is two live
  sessions from one credential. SQLite can prove the sequential half of that; the
  concurrent half is the MySQL integration suite's job.
* ``POST /auth/refresh`` and ``POST /auth/logout`` — the wiring, plus the three
  outcomes that exist only at this layer: a spent token killing its own
  successor, rotation leaving the session deadline exactly where it was, and
  logout being idempotent without becoming an oracle.

The recurring theme: a refresh token is the only credential that survives
overnight, so every test here is really asking "what does an attacker with this
value get?" — and the answer must be "one use, then nothing".
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.refresh import (
    REFRESH_DIGEST_NAMESPACE,
    REFRESH_TOKEN_BYTES,
    mint_refresh_token,
    refresh_expiry,
    refresh_token_hash,
)
from app.db.base import truncate_to_second, utc_now
from app.db.models import RefreshToken, Tenant, User
from app.db.repositories.security import RefreshTokenRepository
from app.db.session import session_scope

LOGIN_PATH = "/api/v1/auth/login"
REFRESH_PATH = "/api/v1/auth/refresh"
LOGOUT_PATH = "/api/v1/auth/logout"
ME_PATH = "/api/v1/auth/me"

#: Matches ``conftest.DEFAULT_PASSWORD``; protects an in-memory database only.
PASSWORD = "correct-horse-battery-staple"  # nosec B105

#: An API contract, spelled out rather than imported so a wording change fails a
#: test instead of being absorbed by the constant it came from.
REFRESH_REFUSED = "Invalid or expired refresh token"


# --- helpers --------------------------------------------------------------
def _login(client: TestClient, tenant_slug: str, email: str, password: str = PASSWORD) -> Response:
    return client.post(
        LOGIN_PATH,
        json={"tenant": tenant_slug, "email": email, "password": password},
    )


def _session(client: TestClient, tenant: Tenant, user: User) -> dict[str, Any]:
    """Log in and return the parsed session body, asserting it was minted."""
    response = _login(client, tenant.slug, user.email)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _refresh(client: TestClient, raw: str) -> Response:
    return client.post(REFRESH_PATH, json={"refresh_token": raw})


def _logout(client: TestClient, raw: str) -> Response:
    return client.post(LOGOUT_PATH, json={"refresh_token": raw})


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _error(response: Response) -> dict[str, Any]:
    payload = response.json()
    assert "error" in payload, payload
    return dict(payload["error"])


def _token_rows(session_factory: sessionmaker[Session]) -> list[RefreshToken]:
    """Every stored refresh row, read in its own transaction."""
    with session_scope(session_factory) as session:
        return list(session.execute(select(RefreshToken)).scalars())


def _seed_token(
    session_factory: sessionmaker[Session],
    user: User,
    *,
    expires_at: datetime | None = None,
    used_at: datetime | None = None,
    revoked_at: datetime | None = None,
    token_epoch: int | None = None,
) -> str:
    """Insert one refresh row directly and return the raw token that opens it.

    Written by hand rather than through ``RefreshTokenRepository.create`` because
    that method prunes rows whose ``expires_at`` has already passed, which is
    exactly the row the expiry tests need to exist.
    """
    raw = mint_refresh_token()
    now = utc_now()
    with session_scope(session_factory) as session:
        session.add(
            RefreshToken(
                user_id=user.id,
                tenant_id=user.tenant_id,
                family_id=uuid.uuid4(),
                token_hash=refresh_token_hash(raw),
                token_epoch=user.token_epoch if token_epoch is None else token_epoch,
                issued_at=truncate_to_second(now - timedelta(days=1)),
                expires_at=(
                    truncate_to_second(now + timedelta(days=6))
                    if expires_at is None
                    else expires_at
                ),
                used_at=used_at,
                revoked_at=revoked_at,
            )
        )
    return raw


def _update_user(session_factory: sessionmaker[Session], user_id: uuid.UUID, **fields: Any) -> None:
    """Mutate one user row in its own committed transaction."""
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


# --- fixtures -------------------------------------------------------------
@pytest.fixture
def make_tenant(session_factory: sessionmaker[Session]) -> Callable[..., Tenant]:
    """Create one committed tenant.

    The ``tenant``/``other_tenant`` fixtures in ``conftest`` are built on the
    shared ``db_session``; these tests deactivate a tenant after the app has
    already read it, so they need their own committed writes.
    """

    def _create(slug: str = "acme", name: str = "Acme Ltd", is_active: bool = True) -> Tenant:
        with session_scope(session_factory) as session:
            owner = Tenant(name=name, slug=slug, is_active=is_active)
            session.add(owner)
            session.flush()
        return owner

    return _create


# --- policy: the token is unguessable -------------------------------------
def test_minting_twice_never_repeats() -> None:
    """A repeat is not a collision to retry; it is a broken source of entropy."""
    assert mint_refresh_token() != mint_refresh_token()


def test_the_token_carries_the_configured_entropy() -> None:
    """``token_urlsafe`` of 32 bytes is 43 characters, and the constant is what
    decides that — so lowering it by accident fails here."""
    token = mint_refresh_token()

    assert len(token) >= 42
    assert REFRESH_TOKEN_BYTES * 8 >= 256


def test_many_tokens_are_all_distinct() -> None:
    """A cheap sanity net: 200 mints colliding would mean a seeded PRNG."""
    assert len({mint_refresh_token() for _ in range(200)}) == 200


# --- policy: the digest ---------------------------------------------------
def test_the_digest_is_a_hex_sha256() -> None:
    digest = refresh_token_hash(mint_refresh_token())

    assert len(digest) == 64
    assert digest == digest.lower()
    assert all(character in "0123456789abcdef" for character in digest)


def test_the_same_token_always_hashes_the_same() -> None:
    """Equality is the only lookup this table performs, so this is load-bearing."""
    token = mint_refresh_token()

    assert refresh_token_hash(token) == refresh_token_hash(token)


def test_different_tokens_hash_differently() -> None:
    assert refresh_token_hash(mint_refresh_token()) != refresh_token_hash(mint_refresh_token())


def test_the_digest_is_not_a_bare_sha256_of_the_token() -> None:
    """The namespace is inside the hashed material, so a digest lifted from this
    table cannot be recomputed by an attacker who knows the token itself."""
    token = mint_refresh_token()

    naive = hashlib.sha256(token.encode("utf-8")).hexdigest()

    assert refresh_token_hash(token) != naive
    assert REFRESH_DIGEST_NAMESPACE == "refresh-token.v1"


def test_the_digest_contains_neither_the_token_nor_the_namespace() -> None:
    """A stored value that embeds its own input is not a digest."""
    token = mint_refresh_token()
    digest = refresh_token_hash(token)

    assert token not in digest
    assert REFRESH_DIGEST_NAMESPACE not in digest


def test_a_token_moved_between_namespaces_cannot_alias() -> None:
    """Bumping the namespace must not collide with an old digest — this is the
    property that lets the scheme change without trusting that no row is live."""
    token = mint_refresh_token()
    old = hashlib.sha256(f"refresh-token.v0\u0000{token}".encode()).hexdigest()

    assert refresh_token_hash(token) != old


# --- policy: the deadline -------------------------------------------------
def test_expiry_adds_the_configured_number_of_days() -> None:
    issued = datetime(2026, 3, 1, 12, 0, 0)

    assert refresh_expiry(issued, 7) == datetime(2026, 3, 8, 12, 0, 0)


def test_expiry_floors_microseconds() -> None:
    """MySQL rounds a microseconds-bearing value *forward*; an expiry a fraction
    of a second in the future is a token that survives a request it should fail."""
    issued = datetime(2026, 3, 1, 12, 0, 0, 999_999)

    assert refresh_expiry(issued, 7) == datetime(2026, 3, 8, 12, 0, 0)
    assert refresh_expiry(issued, 7).microsecond == 0


# --- repository: spending is conditional ----------------------------------
def test_the_first_claim_wins_and_the_second_loses(
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """The gate rotation depends on. A second ``True`` would be two live sessions
    from one token, so this is asserted rather than assumed."""
    tenant = make_tenant()
    user = make_user(tenant)
    now = utc_now()
    with session_scope(session_factory) as session:
        repository = RefreshTokenRepository(session)
        token = repository.create(
            user_id=user.id,
            tenant_id=tenant.id,
            family_id=uuid.uuid4(),
            token_hash=refresh_token_hash("presented-value"),
            token_epoch=user.token_epoch,
            issued_at=now,
            expires_at=refresh_expiry(now, 7),
        )
        digest = token.token_hash

    with session_scope(session_factory) as session:
        assert RefreshTokenRepository(session).claim_for_rotation(digest, now=now) is True

    with session_scope(session_factory) as session:
        assert RefreshTokenRepository(session).claim_for_rotation(digest, now=now) is False


def test_a_revoked_token_cannot_be_claimed(
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """``revoked_at IS NULL`` is part of the predicate, not a courtesy: a stale
    holder of a revoked token must not be able to spend it."""
    tenant = make_tenant()
    user = make_user(tenant)
    now = utc_now()
    with session_scope(session_factory) as session:
        token = RefreshTokenRepository(session).create(
            user_id=user.id,
            tenant_id=tenant.id,
            family_id=uuid.uuid4(),
            token_hash=refresh_token_hash("revoked-value"),
            token_epoch=user.token_epoch,
            issued_at=now,
            expires_at=refresh_expiry(now, 7),
        )
        RefreshTokenRepository(session).revoke_family(token.family_id, now=now)

    with session_scope(session_factory) as session:
        assert (
            RefreshTokenRepository(session).claim_for_rotation(token.token_hash, now=now) is False
        )


def test_revoking_a_family_reaches_every_member_but_no_other(
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """The blast radius of a detected replay, measured: the victim's other tokens
    in *this* family die, and a second session's tokens do not."""
    tenant = make_tenant()
    user = make_user(tenant)
    now = utc_now()
    doomed_family, spared_family = uuid.uuid4(), uuid.uuid4()

    def _issue(family: uuid.UUID, label: str) -> None:
        with session_scope(session_factory) as session:
            RefreshTokenRepository(session).create(
                user_id=user.id,
                tenant_id=tenant.id,
                family_id=family,
                token_hash=refresh_token_hash(label),
                token_epoch=user.token_epoch,
                issued_at=now,
                expires_at=refresh_expiry(now, 7),
            )

    _issue(doomed_family, "first")
    _issue(doomed_family, "second")
    _issue(spared_family, "other-session")

    with session_scope(session_factory) as session:
        assert RefreshTokenRepository(session).revoke_family(doomed_family, now=now) == 2

    revoked = [row.family_id for row in _token_rows(session_factory) if row.revoked_at is not None]

    assert revoked == [doomed_family, doomed_family]


def test_creating_a_token_prunes_only_that_users_expired_rows(
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """Housekeeping must not be able to end somebody else's session."""
    tenant = make_tenant()
    mine = make_user(tenant, email="mine@acme.test")
    theirs = make_user(tenant, email="theirs@acme.test")
    now = utc_now()
    stale = truncate_to_second(now - timedelta(days=30))

    _seed_token(session_factory, theirs, expires_at=stale)
    _seed_token(session_factory, mine, expires_at=stale)

    with session_scope(session_factory) as session:
        RefreshTokenRepository(session).create(
            user_id=mine.id,
            tenant_id=tenant.id,
            family_id=uuid.uuid4(),
            token_hash=refresh_token_hash("fresh"),
            token_epoch=mine.token_epoch,
            issued_at=now,
            expires_at=refresh_expiry(now, 7),
        )

    remaining = _token_rows(session_factory)
    mine_rows = [row for row in remaining if row.user_id == mine.id]
    their_rows = [row for row in remaining if row.user_id == theirs.id]

    # Mine: the stale row is gone, the row that triggered the prune is not.
    assert len(mine_rows) == 1
    assert mine_rows[0].expires_at > truncate_to_second(now)
    # Theirs: untouched, even though it is just as stale.
    assert len(their_rows) == 1
    assert their_rows[0].expires_at == stale


# --- login: the session it hands out --------------------------------------
def test_login_returns_a_refresh_token_beside_the_access_token(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    tenant = make_tenant()
    user = make_user(tenant)

    body = _session(client, tenant, user)

    assert body["refresh_token"]
    assert body["refresh_token"] != body["access_token"]
    # The refresh deadline is in days and the access deadline in minutes, so the
    # ordering is the contract: a session must outlive the token it renews.
    assert datetime.fromisoformat(body["expires_at"]) < datetime.fromisoformat(
        body["refresh_expires_at"]
    )
    assert body["user"]["email"] == user.email


def test_the_refresh_token_is_never_stored_in_the_clear(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """The whole storage argument in one assertion: a dump of this table contains
    nothing a client could present."""
    tenant = make_tenant()
    user = make_user(tenant)

    raw = str(_session(client, tenant, user)["refresh_token"])

    rows = _token_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].token_hash != raw
    assert rows[0].token_hash == refresh_token_hash(raw)
    assert rows[0].used_at is None
    assert rows[0].revoked_at is None


def test_two_logins_are_two_separate_families(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """Signing in on a second device must not join the first device's family, or
    revoking one browser would log the other one out."""
    tenant = make_tenant()
    user = make_user(tenant)

    _session(client, tenant, user)
    _session(client, tenant, user)

    families = {row.family_id for row in _token_rows(session_factory)}

    assert len(families) == 2


# --- refresh: rotation ----------------------------------------------------
def test_refresh_returns_a_working_access_token_and_a_new_refresh_token(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    tenant = make_tenant()
    user = make_user(tenant)
    first = _session(client, tenant, user)

    response = _refresh(client, str(first["refresh_token"]))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["access_token"] != first["access_token"]
    assert body["refresh_token"] != first["refresh_token"]

    # The point of a new access token is that it works.
    me = client.get(ME_PATH, headers=_auth(body["access_token"]))
    assert me.status_code == 200, me.text
    assert me.json()["email"] == user.email


def test_rotation_spends_the_presented_token(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    tenant = make_tenant()
    user = make_user(tenant)
    first = _session(client, tenant, user)

    assert _refresh(client, str(first["refresh_token"])).status_code == 200

    spent = [row for row in _token_rows(session_factory) if row.used_at is not None]
    assert len(spent) == 1


def test_rotation_keeps_the_session_deadline_where_it_was(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """Otherwise a client that refreshed often enough would never be logged out
    and ``REFRESH_TOKEN_EXPIRE_DAYS`` would bound nothing."""
    tenant = make_tenant()
    user = make_user(tenant)
    first = _session(client, tenant, user)

    second = _refresh(client, str(first["refresh_token"])).json()
    third = _refresh(client, str(second["refresh_token"])).json()

    assert second["refresh_expires_at"] == first["refresh_expires_at"]
    assert third["refresh_expires_at"] == first["refresh_expires_at"]


def test_a_successor_joins_the_family_it_replaced(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """Family membership is what makes replay detection reach the successor."""
    tenant = make_tenant()
    user = make_user(tenant)
    first = _session(client, tenant, user)

    _refresh(client, str(first["refresh_token"]))

    families = {row.family_id for row in _token_rows(session_factory)}

    assert len(families) == 1
    assert len(_token_rows(session_factory)) == 2


# --- refresh: reuse is theft ---------------------------------------------
def test_replaying_a_spent_token_is_refused(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    tenant = make_tenant()
    user = make_user(tenant)
    first = _session(client, tenant, user)
    raw = str(first["refresh_token"])
    assert _refresh(client, raw).status_code == 200

    replay = _refresh(client, raw)

    assert replay.status_code == 401, replay.text
    assert _error(replay)["message"] == REFRESH_REFUSED


def test_a_replay_kills_the_successor_too(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """The reason rotation exists. Refusing only the replayed value would leave
    an attacker holding whichever copy they had already rotated in."""
    tenant = make_tenant()
    user = make_user(tenant)
    first = _session(client, tenant, user)
    second = _refresh(client, str(first["refresh_token"])).json()

    assert _refresh(client, str(first["refresh_token"])).status_code == 401

    # The legitimate successor is dead as well — that is the intended cost.
    assert _refresh(client, str(second["refresh_token"])).status_code == 401


def test_a_replay_does_not_touch_another_devices_session(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """Revocation is scoped to the family, so a stolen token cannot become a way
    to log the victim out everywhere."""
    tenant = make_tenant()
    user = make_user(tenant)
    phone = _session(client, tenant, user)
    laptop = _session(client, tenant, user)

    assert _refresh(client, str(phone["refresh_token"])).status_code == 200
    assert _refresh(client, str(phone["refresh_token"])).status_code == 401

    assert _refresh(client, str(laptop["refresh_token"])).status_code == 200


# --- refresh: every refusal is the same refusal ---------------------------
def test_an_unknown_token_is_refused(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    tenant = make_tenant()
    make_user(tenant)

    response = _refresh(client, mint_refresh_token())

    assert response.status_code == 401
    assert _error(response)["message"] == REFRESH_REFUSED


def test_an_empty_token_is_rejected_by_validation(
    client: TestClient,
) -> None:
    """A blank body is a malformed request, not a failed redemption, so it is a
    422 rather than a 401 — the client is wrong, not the credential."""
    response = client.post(REFRESH_PATH, json={"refresh_token": ""})

    assert response.status_code == 422


def test_an_expired_token_is_refused(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    tenant = make_tenant()
    user = make_user(tenant)
    raw = _seed_token(
        session_factory, user, expires_at=truncate_to_second(utc_now() - timedelta(days=30))
    )

    response = _refresh(client, raw)

    assert response.status_code == 401
    assert _error(response)["message"] == REFRESH_REFUSED


def test_a_token_whose_deadline_has_just_been_reached_is_refused(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """The boundary, and the reason the comparison is ``<=`` rather than ``<``:
    a deadline that has been reached is spent, not still usable for the whole
    second it lands in."""
    tenant = make_tenant()
    user = make_user(tenant)
    raw = _seed_token(session_factory, user, expires_at=truncate_to_second(utc_now()))

    assert _refresh(client, raw).status_code == 401


def test_a_seeded_unexpired_token_is_accepted(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """The counterpart to the two refusals above: without it, an expiry check
    that refuses everything would pass every test in this section."""
    tenant = make_tenant()
    user = make_user(tenant)
    raw = _seed_token(session_factory, user)

    response = _refresh(client, raw)

    assert response.status_code == 200, response.text


def test_a_revoked_token_is_refused(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    tenant = make_tenant()
    user = make_user(tenant)
    raw = _seed_token(session_factory, user, revoked_at=truncate_to_second(utc_now()))

    response = _refresh(client, raw)

    assert response.status_code == 401
    assert _error(response)["message"] == REFRESH_REFUSED


def test_a_deactivated_user_cannot_refresh(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """A refresh token that outlived deactivation would make deactivation a
    suggestion — it lasts a week longer than the access token it replaces."""
    tenant = make_tenant()
    user = make_user(tenant)
    raw = str(_session(client, tenant, user)["refresh_token"])

    _update_user(session_factory, user.id, is_active=False)

    assert _refresh(client, raw).status_code == 401


def test_a_deactivated_tenant_cannot_refresh(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    tenant = make_tenant()
    user = make_user(tenant)
    raw = str(_session(client, tenant, user)["refresh_token"])

    _set_tenant_active(session_factory, tenant.id, False)

    assert _refresh(client, raw).status_code == 401


def test_bumping_the_token_epoch_invalidates_a_refresh_token(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """F3's revocation primitive, honoured here too: otherwise "log out
    everywhere" would take effect on the next request for access tokens and a
    week later for sessions."""
    tenant = make_tenant()
    user = make_user(tenant)
    raw = str(_session(client, tenant, user)["refresh_token"])

    _update_user(session_factory, user.id, token_epoch=user.token_epoch + 1)

    assert _refresh(client, raw).status_code == 401


def test_a_deleted_user_cannot_refresh(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """The row is gone, so the token is an orphan credential. Cascading deletes
    remove it in practice; this proves the route does not depend on that."""
    tenant = make_tenant()
    user = make_user(tenant)
    raw = str(_session(client, tenant, user)["refresh_token"])

    with session_scope(session_factory) as session:
        row = session.get(User, user.id)
        assert row is not None
        session.delete(row)

    assert _refresh(client, raw).status_code == 401


def test_a_token_whose_epoch_disagrees_with_its_user_cannot_be_spent(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """The check is against the *stored* row, not against the user's current
    value alone: a row minted under an older epoch must not be honoured just
    because the user is otherwise entitled."""
    tenant = make_tenant()
    user = make_user(tenant)
    raw = _seed_token(session_factory, user, token_epoch=user.token_epoch + 5)

    assert _refresh(client, raw).status_code == 401


# --- logout ---------------------------------------------------------------
def test_logout_ends_the_session(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    tenant = make_tenant()
    user = make_user(tenant)
    first = _session(client, tenant, user)
    raw = str(first["refresh_token"])

    assert _logout(client, raw).status_code == 204
    assert _refresh(client, raw).status_code == 401


def test_logout_kills_the_whole_family(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """Logging out with the *old* token of a rotated pair must still end the
    session — a client that kept a stale reference has asked for exactly that."""
    tenant = make_tenant()
    user = make_user(tenant)
    first = _session(client, tenant, user)
    second = _refresh(client, str(first["refresh_token"])).json()

    assert _logout(client, str(first["refresh_token"])).status_code == 204
    assert _refresh(client, str(second["refresh_token"])).status_code == 401


def test_logout_does_not_need_an_access_token(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """The moment a caller most needs to log out is the moment their access token
    has expired, so this route takes no bearer credential at all."""
    tenant = make_tenant()
    user = make_user(tenant)
    raw = str(_session(client, tenant, user)["refresh_token"])

    response = client.post(LOGOUT_PATH, json={"refresh_token": raw})

    assert response.status_code == 204
    assert "access_token" not in response.text


def test_logout_of_an_unknown_token_is_still_204(
    client: TestClient,
) -> None:
    """A caller who could tell "revoked something" from "revoked nothing" would
    hold a probe for which values were ever issued."""
    response = _logout(client, mint_refresh_token())

    assert response.status_code == 204


def test_logout_is_idempotent(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    tenant = make_tenant()
    user = make_user(tenant)
    raw = str(_session(client, tenant, user)["refresh_token"])

    assert _logout(client, raw).status_code == 204
    assert _logout(client, raw).status_code == 204


def test_logout_leaves_another_devices_session_alone(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    tenant = make_tenant()
    user = make_user(tenant)
    phone = _session(client, tenant, user)
    laptop = _session(client, tenant, user)

    assert _logout(client, str(phone["refresh_token"])).status_code == 204

    assert _refresh(client, str(laptop["refresh_token"])).status_code == 200


# --- the access token is unaffected by refresh ----------------------------
def test_an_access_token_survives_rotation(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """Rotation replaces the refresh token only. Invalidating the old access
    token too would make every client's in-flight requests fail for no gain —
    it expires in minutes anyway."""
    tenant = make_tenant()
    user = make_user(tenant)
    first = _session(client, tenant, user)

    assert _refresh(client, str(first["refresh_token"])).status_code == 200

    assert client.get(ME_PATH, headers=_auth(str(first["access_token"]))).status_code == 200


def test_logout_does_not_invalidate_a_live_access_token(
    client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
) -> None:
    """A documented limit of this milestone, asserted so it cannot be mistaken
    for an oversight: the access token runs to its own expiry (minutes), while
    the refresh token — the credential that would otherwise last a week — is
    dead at once. Closing the remaining window means a server-side denylist,
    which is a separate decision (STATUS.md gap 6 residual)."""
    tenant = make_tenant()
    user = make_user(tenant)
    first = _session(client, tenant, user)

    assert _logout(client, str(first["refresh_token"])).status_code == 204

    assert client.get(ME_PATH, headers=_auth(str(first["access_token"]))).status_code == 200
