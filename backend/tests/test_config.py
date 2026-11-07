"""Fail-fast configuration validation."""

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from app.core.config import DEV_SECRET_PLACEHOLDER, MAX_RETIRED_ENCRYPTION_KEYS, Settings

VALID_PRODUCTION_SECRET = "a" * 40
VALID_PRODUCTION_ORIGINS = "https://app.example.com"
#: Production has to name an absolute document store: the default is relative,
#: which is right for a checkout and wrong for three processes with three working
#: directories (see ``Settings._reject_insecure_production_defaults``).
PRODUCTION_DOCUMENT_PATH = "/var/lib/contract-agent/documents"


def make_production(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "production",
        "secret_key": VALID_PRODUCTION_SECRET,
        "cors_origins": VALID_PRODUCTION_ORIGINS,
        "document_store_path": PRODUCTION_DOCUMENT_PATH,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_development_defaults_are_permissive() -> None:
    settings = Settings(app_env="development", log_json=False)

    assert settings.is_production is False
    assert settings.json_logs is False
    assert settings.secret_key == DEV_SECRET_PLACEHOLDER


def test_json_logs_default_to_on_in_production() -> None:
    settings = make_production()

    assert settings.json_logs is True


def test_explicit_log_json_overrides_environment_default() -> None:
    assert Settings(app_env="test", log_json=True).json_logs is True


def test_production_rejects_placeholder_secret() -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        make_production(secret_key=DEV_SECRET_PLACEHOLDER)


def test_production_rejects_short_secret() -> None:
    with pytest.raises(ValidationError, match="at least 32"):
        make_production(secret_key="short-but-not-placeholder")


def test_production_rejects_plain_text_logs() -> None:
    with pytest.raises(ValidationError, match="LOG_JSON"):
        make_production(log_json=False)


def test_production_rejects_loopback_cors_origin() -> None:
    with pytest.raises(ValidationError, match="loopback"):
        make_production(cors_origins="https://app.example.com,http://localhost:3000")


def test_production_rejects_a_relative_document_store_path() -> None:
    with pytest.raises(ValidationError, match="absolute path"):
        make_production(document_store_path="var/documents")


def test_a_relative_document_store_path_is_fine_in_development() -> None:
    # The default, in the environment that wants documents beside the checkout.
    assert Settings(app_env="development", log_json=False).document_store_path == "var/documents"


def test_wildcard_cors_is_rejected_everywhere() -> None:
    with pytest.raises(ValidationError, match=r"'\*' is not permitted"):
        Settings(app_env="development", cors_origins="*")


def test_invalid_log_level_is_rejected() -> None:
    with pytest.raises(ValidationError, match="LOG_LEVEL"):
        Settings(app_env="development", log_level="LOUD")


def test_log_level_is_normalised() -> None:
    assert Settings(app_env="development", log_level=" debug ").log_level == "DEBUG"


def test_api_prefix_requires_leading_slash() -> None:
    with pytest.raises(ValidationError, match="must start with"):
        Settings(app_env="development", api_v1_prefix="api/v1")


def test_api_prefix_rejects_trailing_slash() -> None:
    with pytest.raises(ValidationError, match="must not end with"):
        Settings(app_env="development", api_v1_prefix="/api/v1/")


def test_access_token_lifetime_is_bounded() -> None:
    """The <=120 minute cap is what keeps access shorter than refresh."""
    with pytest.raises(ValidationError, match="less than or equal to 120"):
        Settings(app_env="development", access_token_expire_minutes=121)


def test_cors_origin_list_is_split_and_trimmed() -> None:
    settings = Settings(app_env="development", cors_origins=" http://a.test , http://b.test, ")

    assert settings.cors_origin_list == ["http://a.test", "http://b.test"]


def test_kafka_servers_are_split() -> None:
    settings = Settings(app_env="development", kafka_bootstrap_servers="a:9092, b:9092")

    assert settings.kafka_servers == ["a:9092", "b:9092"]


def test_encryption_key_shape_detection() -> None:
    # Empty is legal, not valid: the API process never stores field values, so a
    # missing key must not stop it booting. ``build_cipher`` refuses at the point
    # of use instead (fail-closed).
    assert Settings(app_env="development").fernet_key_is_valid is False

    # 32 raw bytes, url-safe base64 encoded -> structurally a valid Fernet key.
    valid_key = base64.urlsafe_b64encode(bytes(32)).decode("ascii")
    assert Settings(app_env="development", encryption_key=valid_key).fernet_key_is_valid is True


def test_a_malformed_encryption_key_is_refused_at_startup() -> None:
    # A malformed key is worse than a missing one: it would fail at the first
    # encrypt, i.e. mid-write, rather than at boot (D7-01).
    with pytest.raises(ValidationError, match="ENCRYPTION_KEY"):
        Settings(app_env="development", encryption_key="not-base64!!")

    # Right alphabet, wrong length.
    short_key = base64.urlsafe_b64encode(bytes(16)).decode("ascii")
    with pytest.raises(ValidationError, match="ENCRYPTION_KEY"):
        Settings(app_env="development", encryption_key=short_key)


# --- retired encryption keys (rotation) -----------------------------------
# ``ENCRYPTION_KEY_PREVIOUS`` is the difference between "rotating the key is
# possible" and "rotating the key silently orphans every stored address". It is
# decrypt-only, so a bad entry cannot corrupt a write — it makes a readable row
# unreadable, which is why it is validated at boot rather than at first use.


def _key(seed: int) -> str:
    """A structurally valid Fernet key, distinct per seed."""
    return base64.urlsafe_b64encode(bytes([seed]) * 32).decode("ascii")


def test_retired_keys_are_split_and_trimmed() -> None:
    settings = Settings(
        app_env="development",
        encryption_key=_key(1),
        encryption_key_previous=f" {_key(2)} , {_key(3)}, ",
    )

    assert settings.retired_encryption_keys == [_key(2), _key(3)]


def test_no_retired_keys_is_the_steady_state() -> None:
    assert Settings(app_env="development", encryption_key=_key(1)).retired_encryption_keys == []


def test_a_malformed_retired_key_is_refused_at_startup() -> None:
    # Named by position, never by value: an old key is still a secret.
    with pytest.raises(ValidationError, match="entry 2"):
        Settings(
            app_env="development",
            encryption_key=_key(1),
            encryption_key_previous=f"{_key(2)}, not-a-key",
        )


def test_retired_keys_without_a_current_key_are_refused() -> None:
    # Retired keys only decrypt, so this configuration could read rows and never
    # seal one — including the rows a reseal run was about to re-wrap.
    with pytest.raises(ValidationError, match="ENCRYPTION_KEY is empty"):
        Settings(app_env="development", encryption_key="", encryption_key_previous=_key(2))


def test_the_current_key_cannot_also_be_retired() -> None:
    with pytest.raises(ValidationError, match="appears in ENCRYPTION_KEY_PREVIOUS"):
        Settings(
            app_env="development",
            encryption_key=_key(1),
            encryption_key_previous=f"{_key(1)},{_key(2)}",
        )


def test_the_same_retired_key_cannot_be_listed_twice() -> None:
    with pytest.raises(ValidationError, match="same key twice"):
        Settings(
            app_env="development",
            encryption_key=_key(1),
            encryption_key_previous=f"{_key(2)},{_key(2)}",
        )


def test_more_retired_keys_than_the_cap_is_refused() -> None:
    # Every decrypt walks the list, and it sits on the send path. A rotation that
    # is finished carries none, so exceeding the cap means one never finished.
    too_many = ",".join(_key(seed) for seed in range(2, 2 + MAX_RETIRED_ENCRYPTION_KEYS + 1))

    with pytest.raises(ValidationError, match="at most"):
        Settings(
            app_env="development",
            encryption_key=_key(1),
            encryption_key_previous=too_many,
        )


def test_exactly_the_cap_of_retired_keys_is_allowed() -> None:
    allowed = ",".join(_key(seed) for seed in range(2, 2 + MAX_RETIRED_ENCRYPTION_KEYS))

    settings = Settings(
        app_env="development",
        encryption_key=_key(1),
        encryption_key_previous=allowed,
    )

    assert len(settings.retired_encryption_keys) == MAX_RETIRED_ENCRYPTION_KEYS


def test_a_zero_dispatch_budget_is_refused_at_startup() -> None:
    """0 reads as "no retries" and behaves as "never sends".

    ``dispatch_notification`` parks the row when ``attempts >= retry_max`` before
    the transport is reached, so a budget of 0 would park every row on its first
    attempt. It is refused at boot rather than left to look configured.
    """
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        Settings(app_env="development", notification_retry_max=0)


def test_the_scheduler_batch_and_interval_are_bounded() -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        Settings(app_env="development", notification_batch_size=0)
    with pytest.raises(ValidationError, match="less than or equal to 300"):
        Settings(app_env="development", notification_poll_interval_seconds=301)

    # Defaults are usable without configuration, so the worker boots out of the box.
    settings = Settings(app_env="development")
    assert settings.notification_batch_size == 50
    assert settings.notification_poll_interval_seconds == 5.0
