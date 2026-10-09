"""SecretsProvider abstraction — env for dev, AWS/Azure for prod.

Why abstract?
    - Dev uses .env; prod uses AWS Secrets Manager / Azure Key Vault
    - Same interface → no code change between environments
    - M23 implements AWS/Azure providers

"""

from __future__ import annotations

from abc import ABC, abstractmethod

from contract_agent.config import get_settings
from contract_agent.logging_config import get_logger

logger = get_logger(__name__)


class SecretsProvider(ABC):
    """Abstract secrets accessor."""

    @abstractmethod
    def get(self, key: str, default: str | None = None) -> str | None:
        """Fetch a secret by key."""


class EnvSecretsProvider(SecretsProvider):
    """Dev provider — reads from process env (already loaded by pydantic-settings)."""

    def __init__(self) -> None:
        self._settings = get_settings()

    def get(self, key: str, default: str | None = None) -> str | None:
        import os

        return os.environ.get(key, default)


class AwsSecretsProvider(SecretsProvider):  # pragma: no cover — M23
    """Production provider — AWS Secrets Manager. Implemented in M23."""

    def get(self, key: str, default: str | None = None) -> str | None:
        raise NotImplementedError("AwsSecretsProvider implemented in Milestone 23")


class AzureSecretsProvider(SecretsProvider):  # pragma: no cover — M23
    """Production provider — Azure Key Vault. Implemented in M23."""

    def get(self, key: str, default: str | None = None) -> str | None:
        raise NotImplementedError("AzureSecretsProvider implemented in Milestone 23")


def get_secrets_provider() -> SecretsProvider:
    """Factory — picks provider based on SECRETS_PROVIDER env var."""
    provider = get_settings().secrets_provider
    if provider == "env":
        return EnvSecretsProvider()
    if provider == "aws":
        return AwsSecretsProvider()
    if provider == "azure":
        return AzureSecretsProvider()
    logger.warning("unknown_secrets_provider", provider=provider, fallback="env")
    return EnvSecretsProvider()
