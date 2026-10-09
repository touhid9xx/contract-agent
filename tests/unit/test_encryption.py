"""Fernet-based PII encryption tests — actual API.

API surface (from src/contract_agent/security/encryption.py):
    - encrypt(plaintext: str) -> str       (base64 Fernet token)
    - decrypt(token: str) -> str           (plaintext)
    - EncryptedString(length=512)          (SQLAlchemy TypeDecorator)
    - _get_fernet() -> Fernet | None       (lru_cached; None if no key)

Behavior notes:
    - If ENCRYPTION_KEY is empty → encrypt/decrypt are NO-OPs (dev/test).
    - Wrong key / tampered token → InvalidToken raised.
    - Malformed key → ValueError raised (Fernet constructor).
"""

from __future__ import annotations

import base64
from typing import cast

import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import String, TypeDecorator

from contract_agent.config import get_settings
from contract_agent.security import encryption as enc_mod


# ============================================================
# FIXTURES
# ============================================================
@pytest.fixture()
def fernet_key() -> str:
    """Valid Fernet key (base64-urlsafe 32 bytes)."""
    return Fernet.generate_key().decode()


@pytest.fixture()
def with_key(monkeypatch: pytest.MonkeyPatch, fernet_key: str):
    """Set ENCRYPTION_KEY env, clear caches. Restores on teardown."""
    monkeypatch.setenv("ENCRYPTION_KEY", fernet_key)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    enc_mod._get_fernet.cache_clear()  # type: ignore[attr-defined]
    yield fernet_key
    get_settings.cache_clear()  # type: ignore[attr-defined]
    enc_mod._get_fernet.cache_clear()  # type: ignore[attr-defined]


@pytest.fixture()
def without_key(monkeypatch: pytest.MonkeyPatch):
    """Ensure ENCRYPTION_KEY is empty (dev/test no-op mode)."""
    monkeypatch.setenv("ENCRYPTION_KEY", "")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    enc_mod._get_fernet.cache_clear()  # type: ignore[attr-defined]
    yield
    get_settings.cache_clear()  # type: ignore[attr-defined]
    enc_mod._get_fernet.cache_clear()  # type: ignore[attr-defined]


# ============================================================
# _get_fernet()
# ============================================================
def test_get_fernet_returns_none_when_key_missing(without_key: None) -> None:
    """No key → None (dev/test mode)."""
    assert enc_mod._get_fernet() is None


def test_get_fernet_returns_instance_when_key_set(with_key: str) -> None:
    """Valid key → Fernet instance."""
    f = enc_mod._get_fernet()
    assert f is not None
    assert isinstance(f, Fernet)


def test_get_fernet_is_cached(with_key: str) -> None:
    """lru_cache(maxsize=1) → same instance returned."""
    f1 = enc_mod._get_fernet()
    f2 = enc_mod._get_fernet()
    assert f1 is f2


def test_get_fernet_raises_on_invalid_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed key → ValueError bubbles up (Fernet constructor).

    Note: we use a specific exception type (ValueError) rather than
    bare `Exception` to satisfy Ruff's B017 and make intent explicit.
    """
    monkeypatch.setenv("ENCRYPTION_KEY", "not-a-valid-key")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    enc_mod._get_fernet.cache_clear()  # type: ignore[attr-defined]

    with pytest.raises(ValueError):
        enc_mod._get_fernet()

    # cleanup
    get_settings.cache_clear()  # type: ignore[attr-defined]
    enc_mod._get_fernet.cache_clear()  # type: ignore[attr-defined]


# ============================================================
# encrypt() / decrypt() — WITH key
# ============================================================
def test_encrypt_decrypt_roundtrip(with_key: str) -> None:
    """encrypt → decrypt returns original."""
    plaintext = "user@example.com"
    token = enc_mod.encrypt(plaintext)
    assert token != plaintext
    assert enc_mod.decrypt(token) == plaintext


def test_encrypt_returns_base64_token(with_key: str) -> None:
    """Output is base64-urlsafe (Fernet token format)."""
    token = enc_mod.encrypt("hello")
    # Fernet tokens start with version byte 0x80 → base64 starts with 'gAAAAA'
    assert token.startswith("gAAAAA")
    # Can be base64-decoded without error
    base64.urlsafe_b64decode(token.encode())


def test_encrypt_different_ciphertext_each_time(with_key: str) -> None:
    """Fernet includes IV + timestamp → same plaintext = different tokens."""
    t1 = enc_mod.encrypt("same")
    t2 = enc_mod.encrypt("same")
    assert t1 != t2  # non-deterministic


def test_encrypt_decrypt_unicode(with_key: str) -> None:
    """Bangla + emoji survive round-trip."""
    plaintext = "নাম: রহিম 🎉"
    assert enc_mod.decrypt(enc_mod.encrypt(plaintext)) == plaintext


def test_encrypt_decrypt_empty_string(with_key: str) -> None:
    """Empty string is encryptable."""
    assert enc_mod.decrypt(enc_mod.encrypt("")) == ""


def test_encrypt_decrypt_long_text(with_key: str) -> None:
    """10_000-char text survives round-trip."""
    plaintext = "A" * 10_000
    assert enc_mod.decrypt(enc_mod.encrypt(plaintext)) == plaintext


# ============================================================
# TAMPER / WRONG KEY
# ============================================================
def test_decrypt_with_wrong_key_raises_invalid_token(
    monkeypatch: pytest.MonkeyPatch, fernet_key: str
) -> None:
    """Encrypt with key A, decrypt with key B → InvalidToken."""
    # Encrypt with key A
    monkeypatch.setenv("ENCRYPTION_KEY", fernet_key)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    enc_mod._get_fernet.cache_clear()  # type: ignore[attr-defined]
    token = enc_mod.encrypt("secret")

    # Switch to key B
    other_key = Fernet.generate_key().decode()
    monkeypatch.setenv("ENCRYPTION_KEY", other_key)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    enc_mod._get_fernet.cache_clear()  # type: ignore[attr-defined]

    with pytest.raises(InvalidToken):
        enc_mod.decrypt(token)

    # cleanup
    get_settings.cache_clear()  # type: ignore[attr-defined]
    enc_mod._get_fernet.cache_clear()  # type: ignore[attr-defined]


def test_decrypt_tampered_token_raises_invalid_token(with_key: str) -> None:
    """Modified token → InvalidToken."""
    token = enc_mod.encrypt("secret")
    tampered = token[:-5] + ("A" if token[-5] != "A" else "B") + token[-4:]

    with pytest.raises(InvalidToken):
        enc_mod.decrypt(tampered)


# ============================================================
# NO-OP MODE (key missing)
# ============================================================
def test_encrypt_noop_when_key_missing(without_key: None) -> None:
    """No key → encrypt returns plaintext unchanged (dev mode)."""
    assert enc_mod.encrypt("plain") == "plain"


def test_decrypt_noop_when_key_missing(without_key: None) -> None:
    """No key → decrypt returns input unchanged (dev mode)."""
    assert enc_mod.decrypt("anything") == "anything"


# ============================================================
# EncryptedString TypeDecorator
# ============================================================
def test_encrypted_string_is_type_decorator() -> None:
    """EncryptedString subclasses TypeDecorator."""
    assert issubclass(enc_mod.EncryptedString, TypeDecorator)


def test_encrypted_string_cache_ok() -> None:
    """EncryptedString declares cache_ok=True."""
    assert enc_mod.EncryptedString.cache_ok is True


def test_encrypted_string_impl_is_string() -> None:
    """Class-level impl is String (SQLAlchemy)."""
    assert enc_mod.EncryptedString.impl is String


def test_encrypted_string_default_length() -> None:
    """Default length is 512 (via String impl instance)."""
    et = enc_mod.EncryptedString()
    impl = cast(String, et.impl)
    assert impl.length == 512


def test_encrypted_string_custom_length() -> None:
    """Custom length is respected."""
    et = enc_mod.EncryptedString(1024)
    impl = cast(String, et.impl)
    assert impl.length == 1024


# ============================================================
# TypeDecorator — process_bind_param
# ============================================================
def test_bind_param_none_returns_none() -> None:
    """None value → None (SQLAlchemy NULL)."""
    et = enc_mod.EncryptedString()
    assert et.process_bind_param(None, None) is None


def test_bind_param_encrypts_string(with_key: str) -> None:
    """String → encrypted token."""
    et = enc_mod.EncryptedString()
    out = et.process_bind_param("hello", None)
    assert out is not None
    assert out != "hello"
    assert enc_mod.decrypt(out) == "hello"


def test_bind_param_coerces_non_string(with_key: str) -> None:
    """Non-string value → str() then encrypt."""
    et = enc_mod.EncryptedString()
    out = et.process_bind_param(12345, None)
    assert out is not None
    assert enc_mod.decrypt(out) == "12345"


# ============================================================
# TypeDecorator — process_result_value
# ============================================================
def test_result_value_none_returns_none() -> None:
    """None DB value → None."""
    et = enc_mod.EncryptedString()
    assert et.process_result_value(None, None) is None


def test_result_value_decrypts(with_key: str) -> None:
    """Encrypted token → decrypted plaintext."""
    et = enc_mod.EncryptedString()
    token = enc_mod.encrypt("secret")
    assert et.process_result_value(token, None) == "secret"


# ============================================================
# Full round-trip via TypeDecorator
# ============================================================
def test_roundtrip_via_type_decorator(with_key: str) -> None:
    """Full cycle: bind_param → result_value → original."""
    et = enc_mod.EncryptedString()
    original = "user@example.com"
    stored = et.process_bind_param(original, None)
    recovered = et.process_result_value(stored, None)
    assert recovered == original
