"""Encryption at rest for extracted field values (F7).

The property under test is *fail-closed*: a process that cannot encrypt must
refuse to write, and a ciphertext that has been tampered with must fail loudly
without putting the plaintext (or the token) into the error message — an error
string is the easiest way for PII to reach a log.
"""

from __future__ import annotations

import base64

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.core.config import Settings
from app.core.crypto import (
    DecryptionError,
    FieldCipher,
    MissingEncryptionKeyError,
    build_cipher,
)

PLAINTEXT = "a.rahman@example.test"


def make_settings(**overrides: object) -> Settings:
    return Settings(app_env="test", log_json=False, **overrides)


@pytest.fixture
def key() -> str:
    return Fernet.generate_key().decode("ascii")


@pytest.fixture
def cipher(key: str) -> FieldCipher:
    return FieldCipher(key)


# --- the cipher itself ----------------------------------------------------
def test_a_value_round_trips_unchanged(cipher: FieldCipher) -> None:
    assert cipher.decrypt(cipher.encrypt(PLAINTEXT)) == PLAINTEXT


def test_a_value_survives_a_non_ascii_round_trip(cipher: FieldCipher) -> None:
    value = "Höhestraße 12, München — 80331"

    assert cipher.decrypt(cipher.encrypt(value)) == value


def test_the_ciphertext_does_not_contain_the_plaintext(cipher: FieldCipher) -> None:
    # The point of the column: a database dump or a log line shows this instead.
    token = cipher.encrypt(PLAINTEXT)

    assert PLAINTEXT not in token
    assert "example" not in token


def test_the_same_value_encrypts_differently_every_time(cipher: FieldCipher) -> None:
    # Fernet carries an IV and a timestamp, so two rows holding the same address
    # cannot be spotted by comparing ciphertexts.
    first = cipher.encrypt(PLAINTEXT)
    second = cipher.encrypt(PLAINTEXT)

    assert first != second
    assert cipher.decrypt(first) == cipher.decrypt(second) == PLAINTEXT


def test_a_different_key_cannot_read_the_token(cipher: FieldCipher) -> None:
    other = FieldCipher(Fernet.generate_key().decode("ascii"))

    with pytest.raises(DecryptionError):
        other.decrypt(cipher.encrypt(PLAINTEXT))


def test_a_tampered_token_fails_loudly_without_echoing_it(cipher: FieldCipher) -> None:
    token = cipher.encrypt(PLAINTEXT)
    # Byte 10 is inside the timestamp: the HMAC no longer matches.
    replacement = "A" if token[10] != "A" else "B"
    tampered = f"{token[:10]}{replacement}{token[11:]}"

    with pytest.raises(DecryptionError) as caught:
        cipher.decrypt(tampered)

    message = str(caught.value)
    assert PLAINTEXT not in message
    assert tampered not in message


# --- building it ----------------------------------------------------------
def test_build_cipher_refuses_without_a_key() -> None:
    with pytest.raises(MissingEncryptionKeyError) as caught:
        build_cipher(make_settings(encryption_key=""))

    # The message has to tell an operator what to set, not just that it is unset.
    assert "ENCRYPTION_KEY" in str(caught.value)


def test_build_cipher_accepts_a_generated_key(key: str) -> None:
    cipher = build_cipher(make_settings(encryption_key=key))

    assert cipher.decrypt(cipher.encrypt(PLAINTEXT)) == PLAINTEXT


def test_a_malformed_key_is_rejected_at_startup() -> None:
    # A typo'd key would otherwise surface at the first extraction, which is a
    # write path at 2am rather than a boot failure.
    with pytest.raises(ValidationError):
        make_settings(encryption_key="not-a-fernet-key")


def test_a_key_of_the_wrong_length_is_rejected_at_startup() -> None:
    short = base64.urlsafe_b64encode(b"0123456789abcdef").decode("ascii")

    with pytest.raises(ValidationError):
        make_settings(encryption_key=short)
