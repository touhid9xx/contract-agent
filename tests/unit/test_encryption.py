"""Encryption round-trip tests."""

from __future__ import annotations

from contract_agent.security.encryption import EncryptedString, decrypt, encrypt


def test_encrypt_decrypt_roundtrip() -> None:
    plain = "alice@example.com"
    token = encrypt(plain)
    assert token != plain
    assert decrypt(token) == plain


def test_encrypted_string_type_roundtrip() -> None:
    t = EncryptedString(512)
    bound = t.process_bind_param("secret@x.com", None)
    assert bound != "secret@x.com"
    result = t.process_result_value(bound, None)
    assert result == "secret@x.com"


def test_encrypted_string_none_safe() -> None:
    t = EncryptedString(512)
    assert t.process_bind_param(None, None) is None
    assert t.process_result_value(None, None) is None
