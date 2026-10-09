"""Secrets provider tests — env default, factory dispatch, ValidationError.

Why these tests?
    - EnvSecretsProvider reads from os.environ via .get()
    - get_secrets_provider() returns env provider by default
    - Invalid SECRETS_PROVIDER values REJECTED at startup (fail-safe)

NOTE: The SecretsProvider contract is `get(name) -> str | None`.
No `get_required()` in current implementation.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from contract_agent.config import get_settings
from contract_agent.security.secrets import (
    EnvSecretsProvider,
    SecretsProvider,
    get_secrets_provider,
)


# ============================================================
# FACTORY
# ============================================================
def test_get_provider_returns_env_by_default() -> None:
    provider = get_secrets_provider()
    assert isinstance(provider, EnvSecretsProvider)


def test_get_provider_returns_same_class_on_repeat() -> None:
    p1 = get_secrets_provider()
    p2 = get_secrets_provider()
    assert type(p1) is type(p2)


# ============================================================
# ABSTRACT BASE
# ============================================================
def test_provider_is_abstract() -> None:
    """SecretsProvider cannot be instantiated directly."""
    with pytest.raises(TypeError):
        SecretsProvider()  # type: ignore[abstract]


def test_provider_has_get_method() -> None:
    """Contract: get() exists."""
    assert hasattr(SecretsProvider, "get")
    assert callable(SecretsProvider.get)


# ============================================================
# ENV PROVIDER — happy path
# ============================================================
def test_env_provider_reads_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_TEST_SECRET", "s3cr3t-value")

    provider = EnvSecretsProvider()
    assert provider.get("MY_TEST_SECRET") == "s3cr3t-value"


def test_env_provider_reads_unicode_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNICODE_SECRET", "বাংলা-টোকেন")

    provider = EnvSecretsProvider()
    assert provider.get("UNICODE_SECRET") == "বাংলা-টোকেন"


# ============================================================
# ENV PROVIDER — missing secret
# ============================================================
def test_env_provider_returns_none_for_missing() -> None:
    provider = EnvSecretsProvider()
    assert provider.get("DEFINITELY_NOT_SET_XYZ_12345") is None


def test_env_provider_returns_none_for_empty_name() -> None:
    """Empty env var name → None (no crash)."""
    provider = EnvSecretsProvider()
    assert provider.get("") is None


# ============================================================
# INVALID PROVIDER (fail-safe)
# ============================================================
def test_unknown_provider_rejected_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SECRETS_PROVIDER=unknown → ValidationError (not silent fallback)."""
    monkeypatch.setenv("SECRETS_PROVIDER", "unknown")

    get_settings.cache_clear()  # type: ignore[attr-defined]

    with pytest.raises(ValidationError) as exc_info:
        get_settings()

    error_str = str(exc_info.value)
    assert "SECRETS_PROVIDER" in error_str or "secrets_provider" in error_str
