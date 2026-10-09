"""Settings validation tests."""

from __future__ import annotations

import base64
from typing import Any

import pytest
from pydantic import ValidationError

from contract_agent.config import Settings


def _base_env() -> dict[str, Any]:
    """Minimal valid env for Settings.

    Why dict[str, Any]?
        Pydantic's Settings(**env) expects typed kwargs. Using `object`
        would trigger Pylance "object is not assignable to str/Literal/bool"
        errors. `Any` tells Pylance to trust the values — they are validated
        by Pydantic at runtime.
    """
    return {
        "JWT_SECRET_KEY": "x" * 64,
        "ENCRYPTION_KEY": base64.urlsafe_b64encode(b"0" * 32).decode(),
    }


# ============================================================
# HAPPY PATH
# ============================================================
def test_settings_loads_with_minimal_env() -> None:
    s = Settings(**_base_env())
    assert s.app_name == "contract-agent"
    assert s.jwt_algorithm == "HS256"


# ============================================================
# JWT_SECRET_KEY
# ============================================================
def test_jwt_secret_too_short_rejected() -> None:
    env = _base_env()
    env["JWT_SECRET_KEY"] = "short"
    with pytest.raises(ValidationError) as exc:
        Settings(**env)
    assert "JWT_SECRET_KEY" in str(exc.value)


# ============================================================
# ENCRYPTION_KEY
# ============================================================
def test_encryption_key_wrong_length_rejected() -> None:
    env = _base_env()
    env["ENCRYPTION_KEY"] = base64.urlsafe_b64encode(b"short").decode()
    with pytest.raises(ValidationError):
        Settings(**env)


def test_encryption_key_invalid_base64_rejected() -> None:
    env = _base_env()
    env["ENCRYPTION_KEY"] = "not-base64-@@@"
    with pytest.raises(ValidationError):
        Settings(**env)


def test_encryption_key_empty_allowed_in_dev() -> None:
    env = _base_env()
    env["ENCRYPTION_KEY"] = ""
    s = Settings(**env)
    assert s.encryption_key == ""


# ============================================================
# NUMERIC VALIDATORS
# ============================================================
def test_confidence_threshold_out_of_range_rejected() -> None:
    env = _base_env()
    env["EXTRACT_CONFIDENCE_THRESHOLD"] = 1.5
    with pytest.raises(ValidationError):
        Settings(**env)


def test_ab_split_out_of_range_rejected() -> None:
    env = _base_env()
    env["PROMPT_AB_SPLIT"] = -0.1
    with pytest.raises(ValidationError):
        Settings(**env)


def test_cron_hour_out_of_range_rejected() -> None:
    env = _base_env()
    env["SCHEDULE_CRON_HOUR_UTC"] = 99
    with pytest.raises(ValidationError):
        Settings(**env)


# ============================================================
# DERIVED PROPERTIES
# ============================================================
def test_database_url_composition() -> None:
    env = _base_env()
    env.update(
        {
            "MYSQL_HOST": "db.example.com",
            "MYSQL_PORT": 3307,
            "MYSQL_USER": "alice",
            "MYSQL_PASSWORD": "secret",
            "MYSQL_DB": "contracts",
        }
    )
    s = Settings(**env)
    assert (
        s.database_url
        == "mysql+pymysql://alice:secret@db.example.com:3307/contracts?charset=utf8mb4"
    )


def test_cors_origins_parsing() -> None:
    env = _base_env()
    env["API_CORS_ORIGINS"] = "http://a.com, http://b.com ,http://c.com"
    s = Settings(**env)
    assert s.cors_origins_list == ["http://a.com", "http://b.com", "http://c.com"]


def test_schedule_offsets_parsing() -> None:
    env = _base_env()
    env["SCHEDULE_OFFSETS_DAYS"] = "60,30,14"
    s = Settings(**env)
    assert s.schedule_offsets_list == [60, 30, 14]


# ============================================================
# ENV FLAGS
# ============================================================
def test_env_flags() -> None:
    env = _base_env()
    env["APP_ENV"] = "prod"
    s = Settings(**env)
    assert s.is_prod
    assert not s.is_dev
    assert not s.is_test
