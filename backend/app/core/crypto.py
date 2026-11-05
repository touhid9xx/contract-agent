"""Encryption at rest for extracted field values (F7).

``ExtractedField.raw_value`` holds the value exactly as the extractor produced it
— an email address, a phone number, a date of birth. That column is the most
sensitive text the system stores, which is why the model docstring has always
said F7 is what encrypts it.

The rule this module exists to enforce is **fail-closed**: a process that cannot
encrypt must refuse to write, not write plaintext and hope a later fix notices.
There is no "encryption disabled" mode, because a mode that stores PII in the
clear is indistinguishable from a bug.

``cryptography`` is already a runtime dependency (PyMySQL needs it for MySQL 8's
``caching_sha2_password``), so the import is unconditional here rather than the
lazy import ``kafka-python`` gets in ``app/workers/publishers.py``.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings

__all__ = [
    "DecryptionError",
    "FieldCipher",
    "MissingEncryptionKeyError",
    "build_cipher",
    "is_sealed",
]


class MissingEncryptionKeyError(RuntimeError):
    """Raised when a PII value would be written with no encryption key.

    This is a configuration error, not a user error: it means the process was
    told to store extracted values but was not given ``ENCRYPTION_KEY``.
    """


class DecryptionError(RuntimeError):
    """Raised when a stored token cannot be decrypted with the current key.

    Almost always a rotated key rather than corruption, and the two need
    different operator responses, so this is kept distinct from a generic
    ``InvalidToken`` and never carries the ciphertext.
    """


class FieldCipher:
    """Symmetric encryption for one field value.

    Fernet is AES-128-CBC with an HMAC-SHA256 tag: authenticated, so a tampered
    or truncated ciphertext fails loudly instead of decrypting to garbage, and
    the token is URL-safe base64 so it survives a TEXT column unchanged.
    """

    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode("ascii"))

    def encrypt(self, value: str) -> str:
        """Encrypt one plaintext value into a storable token."""
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        """Recover the plaintext from a token produced by :meth:`encrypt`.

        The exception deliberately carries no part of the token: these values are
        PII and an error string is the easiest way for PII to reach a log.
        """
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise DecryptionError(
                "stored value could not be decrypted with the configured ENCRYPTION_KEY"
            ) from exc


#: A Fernet token is ``base64url(0x80 || 8-byte timestamp || IV || ciphertext ||
#: HMAC)``. The version byte ``0x80`` plus the always-zero high bytes of a
#: timestamp (every instant before 2106 fits in five) encode to this prefix.
_SEALED_PREFIX = "gAAAAA"
#: 1 version byte + 8 timestamp bytes + 16 IV + 32 HMAC, base64url-encoded.
_SEALED_MIN_LENGTH = 76
_SEALED_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")


def is_sealed(value: str) -> bool:
    """Whether ``value`` is already a Fernet token rather than a plaintext value.

    Recognised by **shape**, deliberately not by attempting a decryption. A value
    that fails to decrypt is ambiguous: it is either plaintext written before gap
    20 was closed, or a token sealed with a key this process does not have. The
    two need opposite responses — seal the first, leave the second strictly alone
    — and re-sealing the second would wrap the ciphertext in a new token and
    destroy the value for good. The token's own format answers the question the
    decryption cannot.

    The test errs in the safe direction: a one-in-a-trillion plaintext that
    happens to look like base64url is encrypted (harmless), while a real token is
    never mistaken for plaintext (which would be data loss).
    """
    if len(value) < _SEALED_MIN_LENGTH or not value.startswith(_SEALED_PREFIX):
        return False
    return all(character in _SEALED_ALPHABET for character in value)


def build_cipher(settings: Settings) -> FieldCipher:
    """Build the process cipher, or refuse.

    ``Settings`` already validates the *shape* of ``ENCRYPTION_KEY`` when it is
    set (``fernet_key_is_valid`` drives a start-up check); this raises when it is
    absent altogether, which is the case at the point of use rather than at
    import, because the API process itself never writes PII.
    """
    if not settings.encryption_key:
        raise MissingEncryptionKeyError(
            "ENCRYPTION_KEY is required to store extracted fields: raw values are "
            "encrypted at rest and there is no plaintext mode"
        )
    return FieldCipher(settings.encryption_key)
