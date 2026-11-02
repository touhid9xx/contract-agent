"""Tests for ``app.core.security`` — hashing and token signing.

These are unit tests with no database and no HTTP layer, because the two
primitives here are where a silent failure is most expensive: a hash that
verifies too much, or a token that decodes when it should not.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.security import (
    DUMMY_PASSWORD,
    JWT_ALGORITHM,
    InvalidTokenError,
    TokenExpiredError,
    decode_access_token,
    dummy_password_hash,
    hash_password,
    issue_access_token,
    password_needs_rehash,
    spend_password_hash_time,
    verify_password,
)
from app.db.enums import UserRole

USER_ID = uuid.uuid4()
TENANT_ID = uuid.uuid4()


def weak_settings(**overrides: object) -> Settings:
    """Cheap Argon2 parameters, so the suite is not dominated by KDF work."""
    values: dict[str, object] = {
        "app_env": "test",
        "log_json": False,
        "argon2_time_cost": 1,
        "argon2_memory_cost": 8192,
        "argon2_parallelism": 1,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


# --- password hashing -----------------------------------------------------
def test_a_hash_verifies_against_its_own_password() -> None:
    settings = weak_settings()
    encoded = hash_password("correct-horse-battery-staple", settings)

    assert verify_password("correct-horse-battery-staple", encoded, settings)


def test_a_hash_rejects_a_different_password() -> None:
    settings = weak_settings()
    encoded = hash_password("correct-horse-battery-staple", settings)

    assert not verify_password("correct-horse-battery-stapl", encoded, settings)


def test_the_same_password_hashes_differently_every_time() -> None:
    """A per-hash salt: equal passwords must not produce equal columns."""
    settings = weak_settings()

    first = hash_password("same-password-both-times", settings)
    second = hash_password("same-password-both-times", settings)

    assert first != second
    assert verify_password("same-password-both-times", first, settings)
    assert verify_password("same-password-both-times", second, settings)


def test_the_stored_hash_is_argon2id_and_never_the_plaintext() -> None:
    settings = weak_settings()
    encoded = hash_password("correct-horse-battery-staple", settings)

    assert encoded.startswith("$argon2id$")
    assert "correct-horse-battery-staple" not in encoded


def test_a_corrupted_hash_reads_as_a_wrong_password() -> None:
    """Damaged data must not become a 500 that names the affected account."""
    settings = weak_settings()

    assert not verify_password("anything-at-all", "not-a-hash", settings)
    assert not verify_password("anything-at-all", "", settings)


def test_a_hash_minted_with_older_parameters_needs_rehashing() -> None:
    old = weak_settings(argon2_time_cost=1)
    new = weak_settings(argon2_time_cost=2)
    encoded = hash_password("correct-horse-battery-staple", old)

    assert not password_needs_rehash(encoded, old)
    assert password_needs_rehash(encoded, new)


def test_verification_uses_the_parameters_stored_in_the_hash() -> None:
    """Raising the configured cost must not lock anyone out existing accounts."""
    strong = Settings(
        app_env="test",
        log_json=False,
        argon2_time_cost=2,
        argon2_memory_cost=16384,
        argon2_parallelism=1,
    )
    encoded = hash_password("correct-horse-battery-staple", strong)

    # A hasher configured for a *different* cost still verifies: the parameters
    # come from the encoded hash, not from settings.
    assert verify_password("correct-horse-battery-staple", encoded, weak_settings())


def test_the_shipped_argon2_defaults_exceed_the_owasp_minimum() -> None:
    """Guards the production defaults, which the test fixture deliberately lowers."""
    defaults = Settings()

    assert defaults.argon2_memory_cost >= 19 * 1024  # 19 MiB
    assert defaults.argon2_time_cost >= 2
    assert defaults.argon2_parallelism >= 1
    assert "$m=65536,t=3,p=4$" in hash_password("correct-horse-battery-staple", defaults)


def test_every_permitted_argon2_shape_satisfies_the_lane_rule() -> None:
    """Argon2 requires ``memory_cost >= 8 * parallelism``.

    The Field bounds make a violation unrepresentable: the memory floor is
    8192 KiB and the parallelism ceiling is 16, so the tightest allowed shape
    still leaves 512 KiB per lane. That is why ``config`` carries no validator
    for this -- it would be unreachable code -- and this test is what keeps the
    arithmetic honest if anyone widens a bound.
    """
    strictest = Settings(argon2_memory_cost=8192, argon2_parallelism=16)

    for config in (Settings(), strictest):
        assert config.argon2_memory_cost >= 8 * config.argon2_parallelism

    # Argon2 itself accepts the tightest permitted shape.
    encoded = hash_password("correct-horse-battery-staple", strictest)
    assert verify_password("correct-horse-battery-staple", encoded, strictest)


def test_too_little_argon2_memory_is_rejected_at_startup() -> None:
    with pytest.raises(ValidationError, match="argon2_memory_cost"):
        Settings(argon2_memory_cost=4096)


def test_the_timing_equaliser_does_real_work() -> None:
    """The dummy hash must be a genuine Argon2id hash of a password nobody has."""
    settings = weak_settings()
    dummy = dummy_password_hash(settings)

    assert dummy.startswith("$argon2id$")
    assert verify_password(DUMMY_PASSWORD, dummy, settings)
    # Must be a no-op that cannot raise: it runs on the "no such user" branch.
    spend_password_hash_time(settings)


# --- tokens ---------------------------------------------------------------
def issue(**overrides: object) -> str:
    values: dict[str, object] = {
        "user_id": USER_ID,
        "tenant_id": TENANT_ID,
        "role": UserRole.OPERATOR,
        "token_epoch": 3,
        "settings": weak_settings(),
    }
    values.update(overrides)
    return issue_access_token(**values).token  # type: ignore[arg-type]


def test_a_token_round_trips_its_claims() -> None:
    settings = weak_settings()
    access = issue_access_token(
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        role=UserRole.OPERATOR,
        token_epoch=3,
        settings=settings,
    )

    claims = decode_access_token(access.token, settings)

    assert claims.user_id == USER_ID
    assert claims.tenant_id == TENANT_ID
    assert claims.role is UserRole.OPERATOR
    assert claims.token_epoch == 3
    assert access.expires_in == settings.access_token_expire_minutes * 60


def test_the_token_header_declares_hs256_only() -> None:
    token = issue()
    header = jwt.get_unverified_header(token)

    assert header["alg"] == JWT_ALGORITHM


def test_an_expired_token_is_rejected_with_its_own_error() -> None:
    settings = weak_settings()
    minted_long_ago = datetime.now(UTC) - timedelta(hours=2)
    access = issue_access_token(
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        role=UserRole.OPERATOR,
        token_epoch=0,
        settings=settings,
        now=minted_long_ago,
    )

    with pytest.raises(TokenExpiredError):
        decode_access_token(access.token, settings)


def test_a_token_signed_with_another_secret_is_rejected() -> None:
    token = issue(settings=weak_settings(secret_key="a-different-secret-key-entirely"))

    with pytest.raises(InvalidTokenError):
        decode_access_token(token, weak_settings())


def test_a_tampered_payload_is_rejected() -> None:
    """Flipping a character in the payload invalidates the signature."""
    token = issue()
    header, payload, signature = token.split(".")
    forged = f"{header}.{payload[:-2]}AA.{signature}"

    with pytest.raises(InvalidTokenError):
        decode_access_token(forged, weak_settings())


def test_an_unsigned_alg_none_token_is_rejected() -> None:
    """The algorithm-confusion case: ``none`` is not in the allow-list."""
    settings = weak_settings()
    forged = jwt.encode(
        {
            "sub": str(USER_ID),
            "tid": str(TENANT_ID),
            "role": UserRole.ADMIN.value,
            "epoch": 0,
            "typ": "access",
            "iss": settings.app_name,
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        },
        "",
        algorithm="none",
    )

    with pytest.raises(InvalidTokenError):
        decode_access_token(forged, settings)


def test_a_token_from_another_issuer_is_rejected() -> None:
    settings = weak_settings()
    token = issue(settings=weak_settings(app_name="some-other-service"))

    with pytest.raises(InvalidTokenError):
        decode_access_token(token, settings)


def test_a_non_access_token_type_is_rejected() -> None:
    """A refresh token must never be replayable as an access token."""
    settings = weak_settings()
    jwt_payload = {
        "sub": str(USER_ID),
        "tid": str(TENANT_ID),
        "role": UserRole.OPERATOR.value,
        "epoch": 0,
        "typ": "refresh",
        "iss": settings.app_name,
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(days=1)).timestamp()),
    }
    other = jwt.encode(jwt_payload, settings.secret_key, algorithm=JWT_ALGORITHM)

    with pytest.raises(InvalidTokenError, match="not an access token"):
        decode_access_token(other, settings)


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("sub", "not-a-uuid"),
        ("tid", "not-a-uuid"),
        ("role", "superuser"),
        ("epoch", "not-an-integer"),
    ],
)
def test_malformed_claims_are_rejected(claim: str, value: str) -> None:
    settings = weak_settings()
    payload = {
        "sub": str(USER_ID),
        "tid": str(TENANT_ID),
        "role": UserRole.OPERATOR.value,
        "epoch": 0,
        "typ": "access",
        "iss": settings.app_name,
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
    }
    payload[claim] = value
    forged = jwt.encode(payload, settings.secret_key, algorithm=JWT_ALGORITHM)

    with pytest.raises(InvalidTokenError):
        decode_access_token(forged, settings)


def test_a_token_without_a_tenant_claim_cannot_be_decoded() -> None:
    """No ``tid`` must never be silently defaulted to "any tenant"."""
    settings = weak_settings()
    payload = {
        "sub": str(USER_ID),
        "role": UserRole.OPERATOR.value,
        "epoch": 0,
        "typ": "access",
        "iss": settings.app_name,
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
    }
    forged = jwt.encode(payload, settings.secret_key, algorithm=JWT_ALGORITHM)

    with pytest.raises(InvalidTokenError):
        decode_access_token(forged, settings)
