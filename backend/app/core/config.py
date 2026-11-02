"""Typed, fail-fast application settings.

Configuration is read once from the process environment (plus an optional local
``.env``) and validated eagerly. Unsafe or contradictory configuration raises at
start-up rather than surfacing later as a half-working service — the spec's
fail-fast requirement. Nothing here ever logs a secret value.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["development", "test", "staging", "production"]

#: The value shipped in .env.example. Never valid in staging/production.
#: Not a credential: production validation below refuses to start when it is
#: still set, which is why the hardcoded-password warning is suppressed.
DEV_SECRET_PLACEHOLDER = "dev-only-insecure-secret-key-change-me"  # noqa: S105  # nosec B105

MIN_PRODUCTION_SECRET_LENGTH = 32
_VALID_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})
#: Hosts considered non-public. Used to *detect* a development origin, never to bind.
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1")  # nosec B104


class Settings(BaseSettings):
    """Validated application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- runtime ---------------------------------------------------------
    app_env: AppEnv = "development"
    app_name: str = "contract-expiry-notification-agent"
    app_version: str = "0.1.0"
    api_v1_prefix: str = "/api/v1"
    log_level: str = "INFO"
    #: ``None`` means "decide from app_env": JSON in staging/production.
    log_json: bool | None = None

    # --- security --------------------------------------------------------
    secret_key: str = Field(default=DEV_SECRET_PLACEHOLDER, min_length=16)
    access_token_expire_minutes: int = Field(default=15, gt=0, le=120)
    refresh_token_expire_days: int = Field(default=7, gt=0, le=30)
    encryption_key: str = ""

    # --- infrastructure --------------------------------------------------
    database_url: str = "mysql+pymysql://app:app@localhost:3306/contracts"
    redis_url: str = "redis://localhost:6379/0"
    kafka_bootstrap_servers: str = "localhost:9092"
    ollama_base_url: str = "http://localhost:11434"

    # --- http ------------------------------------------------------------
    cors_origins: str = "http://localhost:3000"

    # --- validators ------------------------------------------------------
    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in _VALID_LOG_LEVELS:
            allowed = ", ".join(sorted(_VALID_LOG_LEVELS))
            raise ValueError(f"LOG_LEVEL must be one of {allowed}; got {value!r}")
        return level

    @field_validator("api_v1_prefix")
    @classmethod
    def _require_leading_slash(cls, value: str) -> str:
        # Deliberately does not normalise: silently rewriting a misconfigured
        # prefix would hide the mistake. The trailing-slash rule lives in the
        # model validator below so both faults are reported the same way.
        if not value.startswith("/"):
            raise ValueError(f"API_V1_PREFIX must start with '/'; got {value!r}")
        return value

    @field_validator("cors_origins")
    @classmethod
    def _reject_wildcard_cors(cls, value: str) -> str:
        # A wildcard origin combined with credentials is silently ignored by
        # browsers and hides a genuine misconfiguration. Refuse it outright.
        if "*" in value:
            raise ValueError(
                "CORS_ORIGINS must list explicit origins; '*' is not permitted "
                "because credentialed requests are enabled"
            )
        return value

    @model_validator(mode="after")
    def _validate_consistency(self) -> Settings:
        if self.api_v1_prefix.endswith("/"):
            raise ValueError("API_V1_PREFIX must not end with '/'")

        # "access token must expire before the refresh token" is guaranteed
        # structurally by the Field bounds (<=120 minutes vs >=1 day), so no
        # cross-field check is needed; adding one would be unreachable code.

        if self.app_env in {"staging", "production"}:
            self._reject_insecure_production_defaults()

        return self

    def _reject_insecure_production_defaults(self) -> None:
        if self.secret_key == DEV_SECRET_PLACEHOLDER:
            raise ValueError(
                "SECRET_KEY still holds the development placeholder; set a real "
                f"secret in {self.app_env}"
            )
        if len(self.secret_key) < MIN_PRODUCTION_SECRET_LENGTH:
            raise ValueError(
                f"SECRET_KEY must be at least {MIN_PRODUCTION_SECRET_LENGTH} "
                f"characters in {self.app_env}; got {len(self.secret_key)}"
            )
        if self.log_json is False:
            raise ValueError("LOG_JSON must not be disabled in staging/production")
        insecure_origins = [origin for origin in self.cors_origin_list if _is_loopback(origin)]
        if insecure_origins:
            raise ValueError(
                "CORS_ORIGINS must not contain loopback development origins in "
                f"{self.app_env}: {insecure_origins}"
            )

    # --- derived values --------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_test(self) -> bool:
        return self.app_env == "test"

    @property
    def json_logs(self) -> bool:
        """Whether to emit JSON logs. Defaults on for staging/production."""
        if self.log_json is not None:
            return self.log_json
        return self.app_env in {"staging", "production"}

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def kafka_servers(self) -> list[str]:
        return [s.strip() for s in self.kafka_bootstrap_servers.split(",") if s.strip()]

    @property
    def has_encryption_key(self) -> bool:
        return bool(self.encryption_key)

    @property
    def fernet_key_is_valid(self) -> bool:
        """Check ``ENCRYPTION_KEY`` is a 32-byte URL-safe base64 value.

        Fernet itself is only imported where it is used (PII columns), so the
        shape is validated here without pulling ``cryptography`` into every
        process that merely loads settings.
        """
        if not self.encryption_key:
            return False
        try:
            decoded = base64.urlsafe_b64decode(self.encryption_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError):
            return False
        return len(decoded) == 32


def _is_loopback(origin: str) -> bool:
    host = origin.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0]
    return host in _LOOPBACK_HOSTS


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton (validated on first call)."""
    return Settings()
