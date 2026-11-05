"""Encryption at rest for extracted field values (F7).

The property under test is *fail-closed*: a process that cannot encrypt must
refuse to write, and a ciphertext that has been tampered with must fail loudly
without putting the plaintext (or the token) into the error message — an error
string is the easiest way for PII to reach a log.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.core.config import Settings
from app.core.crypto import (
    DecryptionError,
    FieldCipher,
    MissingEncryptionKeyError,
    build_cipher,
    is_sealed,
)

PLAINTEXT = "a.rahman@example.test"


def make_settings(**overrides: Any) -> Settings:
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


# --- telling a token from a plaintext value (gap 20) ----------------------
# ``is_sealed`` answers the one question a failed decryption cannot: is this value
# plaintext that needs sealing, or a token sealed under a key this process does
# not hold? Re-sealing the second destroys it, so the shape test is what protects
# a rotated database from the repair script.
def test_a_real_token_is_recognised(cipher: FieldCipher) -> None:
    assert is_sealed(cipher.encrypt(PLAINTEXT))


def test_a_tampered_token_is_still_recognised_as_a_token(cipher: FieldCipher) -> None:
    # It must be *left alone*, not sealed on top of: decryption will refuse it and
    # the reseal run reports it, which is the correct outcome for a wrong key.
    token = cipher.encrypt(PLAINTEXT)
    replacement = "A" if token[10] != "A" else "B"

    assert is_sealed(f"{token[:10]}{replacement}{token[11:]}")


@pytest.mark.parametrize(
    "value",
    [
        PLAINTEXT,
        "",
        "+442079460958",
        "2025-01-01",
        "1 Test Street",
        # Right prefix and length would be the dangerous case; this one is short.
        "gAAAAA",
        # Long enough and base64url, but the prefix is wrong.
        "A" * 120,
    ],
)
def test_a_plaintext_value_is_not_mistaken_for_a_token(value: str) -> None:
    assert not is_sealed(value)


def test_a_plaintext_that_mimics_a_token_is_left_alone(cipher: FieldCipher) -> None:
    """The error is deliberately one-directional: leave it alone, never seal twice.

    A value can satisfy the shape test without being a real token. Choosing to read
    it as a token costs us a plaintext row that the reseal run silently skips — a
    visible, repairable state, since the column still reads as plaintext. Choosing
    the other way would mean re-sealing a token from a key we no longer hold, which
    overwrites the only copy of the value with ciphertext-of-ciphertext. The
    unrecoverable mistake is the one this direction avoids.

    It is also unreachable for the values this column actually stores: ``@`` and
    ``.`` are outside the base64url alphabet, so an email or E.164 phone can never
    pass the alphabet check.
    """
    looks_like_a_token = "gAAAAA" + "A" * 100

    assert is_sealed(looks_like_a_token) is True
