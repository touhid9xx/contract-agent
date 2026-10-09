"""Fernet-based PII encryption + SQLAlchemy TypeDecorator.

Why Fernet?
    - Symmetric AEAD, key from ENCRYPTION_KEY env var
    - PII columns (email, phone, address, dob) encrypted at rest
    - Never log the key — SecretsProvider abstracts it in prod

"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import String, TypeDecorator

from contract_agent.config import get_settings
from contract_agent.logging_config import get_logger

logger = get_logger(__name__)


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet | None:
    """Return Fernet instance or None if key not configured (dev/test)."""
    key = get_settings().encryption_key
    if not key:
        logger.warning(
            "encryption_key_missing",
            msg="ENCRYPTION_KEY not set — EncryptedString will store PLAINTEXT (dev only!)",
        )
        return None
    try:
        return Fernet(key.encode())
    except Exception as exc:
        logger.error("encryption_key_invalid", error=str(exc))
        raise


def encrypt(plaintext: str) -> str:
    """Encrypt a string. Returns base64 token. No-op if key missing."""
    f = _get_fernet()
    if f is None:
        return plaintext
    return f.encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a token. Returns plaintext. No-op if key missing."""
    f = _get_fernet()
    if f is None:
        return token
    try:
        return f.decrypt(token.encode()).decode()
    except InvalidToken:
        logger.error("decrypt_failed_invalid_token")
        raise


class EncryptedString(TypeDecorator[str]):
    """SQLAlchemy type — transparently encrypts on write, decrypts on read.

    Usage:
        email: Mapped[str] = mapped_column(EncryptedString(512))
    """

    impl = String
    cache_ok = True

    def __init__(self, length: int = 512) -> None:
        # Fernet token is ~1.4x plaintext + overhead — pad length
        super().__init__(length=length)

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            value = str(value)
        return encrypt(value)

    def process_result_value(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return decrypt(value)
