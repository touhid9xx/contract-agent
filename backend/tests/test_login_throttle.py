"""Login admission control (gap 9): the policy, the failure log, and the route.

Three layers are tested separately because each fails in a different way:

* ``decide`` / ``login_key_hash`` — pure functions, so the interesting cases
  (threshold boundary, a ``Retry-After`` that must not be zero, two spellings of
  one address sharing a counter) need no database and no clock.
* ``LoginAttemptRepository`` — the count/oldest query, the self-prune and the
  timestamp flooring, against a real database.
* ``POST /auth/login`` — the wiring, and the one bug this slice exists to
  prevent: the counter must survive the very ``401`` that ends the request. If
  it did not, the limiter would count nothing and read as protection while
  providing none, so that case is proven end to end rather than assumed.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.login_throttle import (
    LOGIN_KEY_NAMESPACE,
    decide,
    login_key_hash,
    window_start,
)
from app.db.models import LoginAttempt, Tenant, User
from app.db.repositories.security import LoginAttemptRepository
from app.db.session import session_scope
from app.main import create_app

LOGIN_PATH = "/api/v1/auth/login"

#: Matches ``conftest.DEFAULT_PASSWORD``; protects an in-memory database only.
PASSWORD = "correct-horse-battery-staple"  # nosec B105

#: Both are API contracts, so they are spelled out here rather than imported: a
#: change to either wording should fail a test instead of being absorbed by the
#: constant it came from.
INVALID_CREDENTIALS = "Invalid tenant, email or password"
TOO_MANY_ATTEMPTS = "Too many failed login attempts; try again later"

#: Small enough that exhausting it costs three requests, not five.
MAX_ATTEMPTS = 3
WINDOW_SECONDS = 900


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


def _attempt_rows(session_factory: sessionmaker[Session]) -> list[LoginAttempt]:
    """Every row in the failure log, read in its own transaction."""
    with session_scope(session_factory) as session:
        return list(session.execute(select(LoginAttempt)).scalars())


def _exhaust(
    client: TestClient, tenant_slug: str, email: str, attempts: int = MAX_ATTEMPTS
) -> None:
    """Spend the budget with wrong passwords, asserting each one is a plain 401."""
    for _ in range(attempts):
        response = _login(client, tenant_slug, email, "not-the-password")
        assert response.status_code == 401, response.text


# --- fixtures -------------------------------------------------------------
@pytest.fixture
def tight_settings(settings: Settings) -> Settings:
    """The conftest settings with a login budget small enough to exhaust.

    ``model_copy`` rather than a fresh ``Settings(...)`` so the lowered Argon2
    cost from ``conftest`` is kept; a full-cost hash here would make this file
    the slowest in the suite without testing anything extra.
    """
    return settings.model_copy(
        update={
            "login_max_attempts": MAX_ATTEMPTS,
            "login_window_seconds": WINDOW_SECONDS,
        }
    )


@pytest.fixture
def throttled_client(
    tight_settings: Settings, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    app = create_app(tight_settings, session_factory=session_factory)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def make_tenant(session_factory: sessionmaker[Session]) -> Callable[..., Tenant]:
    """A committed tenant, so the running app's own session can see it."""

    def _create(slug: str = "acme", name: str = "Acme Ltd") -> Tenant:
        with session_scope(session_factory) as session:
            tenant = Tenant(name=name, slug=slug)
            session.add(tenant)
            session.flush()
            return tenant

    return _create


# --- the key -------------------------------------------------------------
def test_the_key_is_a_hex_sha256_digest() -> None:
    key = login_key_hash("acme", "ops@acme.test")

    assert len(key) == 64
    assert all(char in "0123456789abcdef" for char in key)


def test_the_key_contains_no_address_a_human_can_read() -> None:
    """The failure log must not become a second, unencrypted store of an email.

    Gap 20 sealed ``extracted_fields`` for exactly this reason; a table holding
    addresses in the clear would undo that for the address it holds here.
    """
    key = login_key_hash("acme", "ops@acme.test")

    assert "ops" not in key
    assert "acme.test" not in key
    assert "@" not in key


def test_the_key_cannot_be_confused_with_a_bare_hash_of_the_address() -> None:
    """The namespace is part of the material, so the digest is not reusable.

    Without it the digest would equal the unsalted SHA-256 of a low-entropy
    value, which is guessable from a precomputed table; with it, a digest lifted
    from this table says nothing outside this application.
    """
    bare = hashlib.sha256(b"acme\x00ops@acme.test").hexdigest()

    assert login_key_hash("acme", "ops@acme.test") != bare


def test_the_namespace_is_versioned() -> None:
    """Bumping the constant must move every key, or an old and new scheme alias."""
    assert LOGIN_KEY_NAMESPACE == "login-throttle.v1"


def test_the_same_pair_always_produces_the_same_key() -> None:
    assert login_key_hash("acme", "ops@acme.test") == login_key_hash("acme", "ops@acme.test")


def test_both_parts_fold_the_way_the_login_lookup_folds_them() -> None:
    """Otherwise one account gets two budgets, which doubles the guessing room."""
    canonical = login_key_hash("acme", "ops@acme.test")

    assert login_key_hash("  ACME  ", " OPS@Acme.TEST ") == canonical


def test_a_different_tenant_is_a_different_key() -> None:
    """The same address in two organisations is not the same account."""
    assert login_key_hash("acme", "ops@acme.test") != login_key_hash("globex", "ops@acme.test")


def test_a_different_address_is_a_different_key() -> None:
    assert login_key_hash("acme", "ops@acme.test") != login_key_hash("acme", "other@acme.test")


def test_moving_the_separator_does_not_collide() -> None:
    """The separator is a character neither part may contain, so the three
    components cannot be shifted across the boundary to make equal material."""
    assert login_key_hash("acme", "x@y.test") != login_key_hash("acme\x00x@y.test", "")


# --- window_start ---------------------------------------------------------
def test_the_window_start_is_the_window_length_before_now() -> None:
    now = datetime(2025, 1, 1, 12, 0, 0)

    assert window_start(now, 900) == datetime(2025, 1, 1, 11, 45, 0)


# --- decide ---------------------------------------------------------------
NOW = datetime(2025, 1, 1, 12, 0, 0)


def test_below_the_threshold_the_attempt_proceeds() -> None:
    decision = decide(
        failures=MAX_ATTEMPTS - 1,
        earliest=NOW - timedelta(seconds=10),
        now=NOW,
        max_attempts=MAX_ATTEMPTS,
        window_seconds=WINDOW_SECONDS,
    )

    assert decision.is_blocked is False
    assert decision.retry_after_seconds == 0
    assert decision.failures == MAX_ATTEMPTS - 1


def test_zero_failures_is_admitted() -> None:
    decision = decide(
        failures=0,
        earliest=None,
        now=NOW,
        max_attempts=MAX_ATTEMPTS,
        window_seconds=WINDOW_SECONDS,
    )

    assert decision.is_blocked is False


def test_exactly_at_the_threshold_the_attempt_is_refused() -> None:
    """``max_attempts`` failures are tolerated; the next attempt is refused."""
    decision = decide(
        failures=MAX_ATTEMPTS,
        earliest=NOW - timedelta(seconds=10),
        now=NOW,
        max_attempts=MAX_ATTEMPTS,
        window_seconds=WINDOW_SECONDS,
    )

    assert decision.is_blocked is True
    assert decision.failures == MAX_ATTEMPTS


def test_more_failures_than_the_threshold_is_still_refused() -> None:
    """Concurrent attempts can push the count past the threshold."""
    decision = decide(
        failures=MAX_ATTEMPTS + 4,
        earliest=NOW - timedelta(seconds=10),
        now=NOW,
        max_attempts=MAX_ATTEMPTS,
        window_seconds=WINDOW_SECONDS,
    )

    assert decision.is_blocked is True


def test_retry_after_is_the_time_until_the_oldest_failure_ages_out() -> None:
    decision = decide(
        failures=MAX_ATTEMPTS,
        earliest=NOW - timedelta(seconds=100),
        now=NOW,
        max_attempts=MAX_ATTEMPTS,
        window_seconds=WINDOW_SECONDS,
    )

    assert decision.retry_after_seconds == WINDOW_SECONDS - 100


def test_retry_after_rounds_up_never_down() -> None:
    """Truncating would tell the caller to retry a moment too early, and the
    refusal they got would contradict the advice."""
    decision = decide(
        failures=MAX_ATTEMPTS,
        earliest=NOW - timedelta(seconds=WINDOW_SECONDS - 0.5),
        now=NOW,
        max_attempts=MAX_ATTEMPTS,
        window_seconds=WINDOW_SECONDS,
    )

    assert decision.retry_after_seconds == 1


def test_retry_after_is_never_zero() -> None:
    """A ``Retry-After: 0`` invites an immediate retry loop."""
    decision = decide(
        failures=MAX_ATTEMPTS,
        earliest=NOW - timedelta(seconds=WINDOW_SECONDS + 5),
        now=NOW,
        max_attempts=MAX_ATTEMPTS,
        window_seconds=WINDOW_SECONDS,
    )

    assert decision.retry_after_seconds == 1


def test_a_missing_earliest_timestamp_still_answers_with_a_positive_delay() -> None:
    """Defensive: ``earliest`` is ``None`` only when nothing was counted, but a
    blocked decision must never be able to produce a zero delay."""
    decision = decide(
        failures=MAX_ATTEMPTS,
        earliest=None,
        now=NOW,
        max_attempts=MAX_ATTEMPTS,
        window_seconds=WINDOW_SECONDS,
    )

    assert decision.is_blocked is True
    assert decision.retry_after_seconds >= 1


# --- repository -----------------------------------------------------------
def test_a_recorded_failure_is_counted(session_factory: sessionmaker[Session]) -> None:
    key = login_key_hash("acme", "ops@acme.test")
    now = datetime(2025, 1, 1, 12, 0, 0)

    with session_scope(session_factory) as session:
        LoginAttemptRepository(session).record(key, now=now, prune_before=now)

    with session_scope(session_factory) as session:
        count, earliest = LoginAttemptRepository(session).window(
            key, since=now - timedelta(seconds=WINDOW_SECONDS)
        )

    assert count == 1
    assert earliest == now


def test_the_window_ignores_failures_older_than_it(
    session_factory: sessionmaker[Session],
) -> None:
    """The block must lift by itself; there is no lockout flag to reset."""
    key = login_key_hash("acme", "ops@acme.test")
    now = datetime(2025, 1, 1, 12, 0, 0)
    long_ago = now - timedelta(seconds=WINDOW_SECONDS + 1)

    with session_scope(session_factory) as session:
        LoginAttemptRepository(session).record(
            key, now=long_ago, prune_before=long_ago - timedelta(seconds=WINDOW_SECONDS)
        )

    with session_scope(session_factory) as session:
        count, earliest = LoginAttemptRepository(session).window(
            key, since=now - timedelta(seconds=WINDOW_SECONDS)
        )

    assert count == 0
    assert earliest is None


def test_the_window_counts_only_its_own_key(
    session_factory: sessionmaker[Session],
) -> None:
    mine = login_key_hash("acme", "ops@acme.test")
    theirs = login_key_hash("acme", "other@acme.test")
    now = datetime(2025, 1, 1, 12, 0, 0)

    with session_scope(session_factory) as session:
        repository = LoginAttemptRepository(session)
        repository.record(theirs, now=now, prune_before=now)
        repository.record(theirs, now=now, prune_before=now)

    with session_scope(session_factory) as session:
        count, _ = LoginAttemptRepository(session).window(mine, since=now)

    assert count == 0


def test_recording_prunes_this_keys_expired_rows(
    session_factory: sessionmaker[Session],
) -> None:
    """The table is bounded without a sweeper process."""
    key = login_key_hash("acme", "ops@acme.test")
    now = datetime(2025, 1, 1, 12, 0, 0)
    stale = now - timedelta(seconds=WINDOW_SECONDS + 60)

    with session_scope(session_factory) as session:
        LoginAttemptRepository(session).record(
            key, now=stale, prune_before=stale - timedelta(seconds=WINDOW_SECONDS)
        )

    with session_scope(session_factory) as session:
        LoginAttemptRepository(session).record(
            key, now=now, prune_before=now - timedelta(seconds=WINDOW_SECONDS)
        )

    assert len(_attempt_rows(session_factory)) == 1


def test_the_stored_timestamp_is_floored_to_the_whole_second(
    session_factory: sessionmaker[Session],
) -> None:
    """MySQL's ``DATETIME`` rounds a microseconds-bearing value *forward*.

    A failure stamped a fraction of a second in the future would still be
    counted, but the ``Retry-After`` derived from it would be measured from an
    instant that has not happened yet.
    """
    key = login_key_hash("acme", "ops@acme.test")
    now = datetime(2025, 1, 1, 12, 0, 0, 987_654)

    with session_scope(session_factory) as session:
        LoginAttemptRepository(session).record(key, now=now, prune_before=now)

    rows = _attempt_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].attempted_at.microsecond == 0
    assert rows[0].attempted_at == datetime(2025, 1, 1, 12, 0, 0)


def test_clearing_forgets_only_that_key(session_factory: sessionmaker[Session]) -> None:
    mine = login_key_hash("acme", "ops@acme.test")
    theirs = login_key_hash("acme", "other@acme.test")
    now = datetime(2025, 1, 1, 12, 0, 0)

    with session_scope(session_factory) as session:
        repository = LoginAttemptRepository(session)
        repository.record(mine, now=now, prune_before=now)
        repository.record(theirs, now=now, prune_before=now)

    with session_scope(session_factory) as session:
        LoginAttemptRepository(session).clear(mine)

    with session_scope(session_factory) as session:
        mine_count, _ = LoginAttemptRepository(session).window(mine, since=now)
        theirs_count, _ = LoginAttemptRepository(session).window(theirs, since=now)

    assert mine_count == 0
    assert theirs_count == 1


# --- the route ------------------------------------------------------------
def test_the_route_counts_a_wrong_password(
    throttled_client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
    session_factory: sessionmaker[Session],
) -> None:
    """The counter must survive the 401 it is recorded alongside.

    ``get_request_session`` never commits, so a write on the *request* session
    would be rolled back by the very exception that ends the request — the
    failure log would stay empty forever and the throttle would be decorative.
    """
    tenant = make_tenant()
    make_user(tenant, email="ops@acme.test")

    _exhaust(throttled_client, tenant.slug, "ops@acme.test", attempts=1)

    assert len(_attempt_rows(session_factory)) == 1


def test_the_attempt_after_the_budget_is_refused_with_429(
    throttled_client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    tenant = make_tenant()
    make_user(tenant, email="ops@acme.test")
    _exhaust(throttled_client, tenant.slug, "ops@acme.test")

    response = _login(throttled_client, tenant.slug, "ops@acme.test")

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
    assert response.json()["error"]["message"] == TOO_MANY_ATTEMPTS


def test_a_refusal_carries_a_positive_retry_after(
    throttled_client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    tenant = make_tenant()
    make_user(tenant, email="ops@acme.test")
    _exhaust(throttled_client, tenant.slug, "ops@acme.test")

    response = _login(throttled_client, tenant.slug, "ops@acme.test")

    retry_after = response.headers["Retry-After"]
    assert retry_after.isdigit()
    assert int(retry_after) > 0
    assert int(retry_after) <= WINDOW_SECONDS


def test_the_correct_password_is_refused_while_the_block_is_live(
    throttled_client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    """Admission control runs before the password is checked, by design.

    Otherwise an attacker with the right password would be admitted during a
    flood, and the limiter would be worth nothing to the account being attacked.
    """
    tenant = make_tenant()
    make_user(tenant, email="ops@acme.test")
    _exhaust(throttled_client, tenant.slug, "ops@acme.test")

    response = _login(throttled_client, tenant.slug, "ops@acme.test", PASSWORD)

    assert response.status_code == 429


def test_a_refused_attempt_does_not_grow_the_log(
    throttled_client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
    session_factory: sessionmaker[Session],
) -> None:
    """A flood of blocked requests must not become a write amplifier."""
    tenant = make_tenant()
    make_user(tenant, email="ops@acme.test")
    _exhaust(throttled_client, tenant.slug, "ops@acme.test")
    before = len(_attempt_rows(session_factory))

    for _ in range(5):
        assert _login(throttled_client, tenant.slug, "ops@acme.test").status_code == 429

    assert len(_attempt_rows(session_factory)) == before == MAX_ATTEMPTS


def test_an_unknown_account_is_refused_in_exactly_the_same_way(
    throttled_client: TestClient, make_tenant: Callable[..., Tenant]
) -> None:
    """The limiter must not become an oracle for which addresses exist.

    The counter is keyed on the submitted pair alone, so a fictional address
    reaches the same 429 with the same words as a real one.
    """
    tenant = make_tenant()
    _exhaust(throttled_client, tenant.slug, "nobody@acme.test")

    response = _login(throttled_client, tenant.slug, "nobody@acme.test")

    assert response.status_code == 429
    assert response.json()["error"]["message"] == TOO_MANY_ATTEMPTS


def test_an_unknown_account_and_a_real_one_are_indistinguishable(
    throttled_client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    tenant = make_tenant()
    make_user(tenant, email="ops@acme.test")
    _exhaust(throttled_client, tenant.slug, "ops@acme.test")
    _exhaust(throttled_client, tenant.slug, "nobody@acme.test")

    real = _login(throttled_client, tenant.slug, "ops@acme.test")
    fictional = _login(throttled_client, tenant.slug, "nobody@acme.test")

    assert real.status_code == fictional.status_code == 429
    assert real.json()["error"]["message"] == fictional.json()["error"]["message"]


def test_an_unknown_slug_is_throttled_too(
    throttled_client: TestClient, make_tenant: Callable[..., Tenant]
) -> None:
    """A wrong slug has no tenant row to count against, which is why the table
    is not tenant-scoped. Counting only real tenants would leak their existence."""
    make_tenant()

    _exhaust(throttled_client, "no-such-tenant", "ops@acme.test")

    response = _login(throttled_client, "no-such-tenant", "ops@acme.test")

    assert response.status_code == 429


def test_another_account_is_unaffected(
    throttled_client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    """The control is per-account: one colleague's typos must not lock out
    everyone else in the organisation."""
    tenant = make_tenant()
    make_user(tenant, email="ops@acme.test")
    make_user(tenant, email="other@acme.test")
    _exhaust(throttled_client, tenant.slug, "ops@acme.test")

    response = _login(throttled_client, tenant.slug, "other@acme.test")

    assert response.status_code == 200


def test_the_same_address_in_another_tenant_has_its_own_budget(
    throttled_client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    acme = make_tenant(slug="acme", name="Acme Ltd")
    globex = make_tenant(slug="globex", name="Globex Ltd")
    make_user(acme, email="ops@acme.test")
    make_user(globex, email="ops@acme.test")
    _exhaust(throttled_client, acme.slug, "ops@acme.test")

    response = _login(throttled_client, globex.slug, "ops@acme.test")

    assert response.status_code == 200


def test_a_different_spelling_of_the_address_shares_the_budget(
    throttled_client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    """Case and surrounding spaces must not buy a second allowance."""
    tenant = make_tenant()
    make_user(tenant, email="ops@acme.test")
    _exhaust(throttled_client, tenant.slug, " OPS@Acme.TEST ")

    response = _login(throttled_client, tenant.slug, "ops@acme.test")

    assert response.status_code == 429


def test_a_successful_login_clears_the_failures_behind_it(
    throttled_client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    """Without the clear, a user who mistyped twice and then got in would start
    their next session one slip away from a lockout they never earned."""
    tenant = make_tenant()
    make_user(tenant, email="ops@acme.test")
    _exhaust(throttled_client, tenant.slug, "ops@acme.test", attempts=MAX_ATTEMPTS - 1)
    assert _login(throttled_client, tenant.slug, "ops@acme.test").status_code == 200

    _exhaust(throttled_client, tenant.slug, "ops@acme.test", attempts=MAX_ATTEMPTS - 1)
    response = _login(throttled_client, tenant.slug, "ops@acme.test")

    assert response.status_code == 200


def test_a_successful_login_leaves_no_attempt_rows(
    throttled_client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
    session_factory: sessionmaker[Session],
) -> None:
    tenant = make_tenant()
    make_user(tenant, email="ops@acme.test")
    _exhaust(throttled_client, tenant.slug, "ops@acme.test", attempts=MAX_ATTEMPTS - 1)

    assert _login(throttled_client, tenant.slug, "ops@acme.test").status_code == 200

    assert _attempt_rows(session_factory) == []


def test_the_failure_log_stores_no_address(
    throttled_client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
    session_factory: sessionmaker[Session],
) -> None:
    """End to end, the row must be a digest and nothing else."""
    tenant = make_tenant()
    make_user(tenant, email="ops@acme.test")
    _exhaust(throttled_client, tenant.slug, "ops@acme.test")

    rows = _attempt_rows(session_factory)
    assert len(rows) == MAX_ATTEMPTS
    for row in rows:
        assert "@" not in row.key_hash
        assert "ops" not in row.key_hash
        assert row.key_hash == login_key_hash(tenant.slug, "ops@acme.test")


def test_every_failure_is_stamped_with_a_whole_second(
    throttled_client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
    session_factory: sessionmaker[Session],
) -> None:
    """The route passes a microsecond-bearing clock; the column must not keep it.

    MySQL rounds such a value *forward*, so the failure would be stamped a
    fraction of a second in the future and the ``Retry-After`` window would
    start from an instant that has not happened yet.
    """
    tenant = make_tenant()
    make_user(tenant, email="ops@acme.test")
    _exhaust(throttled_client, tenant.slug, "ops@acme.test")

    rows = _attempt_rows(session_factory)

    assert len(rows) == MAX_ATTEMPTS
    assert all(row.attempted_at.microsecond == 0 for row in rows)


def test_a_401_before_the_threshold_still_says_nothing_specific(
    throttled_client: TestClient, make_tenant: Callable[..., Tenant], make_user: Callable[..., User]
) -> None:
    """Admission control must not soften the credential message it precedes."""
    tenant = make_tenant()
    make_user(tenant, email="ops@acme.test")

    wrong_password = _login(throttled_client, tenant.slug, "ops@acme.test", "nope")
    unknown_user = _login(throttled_client, tenant.slug, "nobody@acme.test")
    unknown_tenant = _login(throttled_client, "no-such-tenant", "ops@acme.test")

    for response in (wrong_password, unknown_user, unknown_tenant):
        assert response.status_code == 401
        assert response.json()["error"]["message"] == INVALID_CREDENTIALS


def test_a_minted_user_id_is_not_reachable_through_the_failure_log(
    throttled_client: TestClient,
    make_tenant: Callable[..., Tenant],
    make_user: Callable[..., User],
    session_factory: sessionmaker[Session],
) -> None:
    """A row must not carry an identifier that maps back to an account."""
    tenant = make_tenant()
    user = make_user(tenant, email="ops@acme.test")
    _exhaust(throttled_client, tenant.slug, "ops@acme.test")

    rows = _attempt_rows(session_factory)
    rendered = " ".join(str(value) for row in rows for value in vars(row).values())

    assert str(user.id) not in rendered
    assert str(tenant.id) not in rendered
    assert isinstance(rows[0].id, uuid.UUID)
