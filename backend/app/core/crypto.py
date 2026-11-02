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
