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

from collections.abc import Sequence

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

    The cipher holds a **key ring**: exactly one key seals, and any number of
    retired keys can still unseal. Without that, the first rotation of
    ``ENCRYPTION_KEY`` turns every stored recipient into an unreadable value and
    the only recovery is a database restore — which is why the ring exists rather
    than a note telling operators not to rotate. Retired keys are decrypt-only by
    construction: :meth:`encrypt` has no way to select one.
    """

    def __init__(self, key: str, retired_keys: Sequence[str] = ()) -> None:
        self._fernet = Fernet(key.encode("ascii"))
        self._retired = tuple(Fernet(retired.encode("ascii")) for retired in retired_keys)

    @property
    def has_retired_keys(self) -> bool:
        """Whether this process can still read values sealed under an older key."""
        return bool(self._retired)

    def encrypt(self, value: str) -> str:
        """Encrypt one plaintext value into a storable token.

        Always with the current key, never a retired one: a rotation that kept
        writing old tokens would never finish.
        """
        return self.encrypt_bytes(value.encode("utf-8")).decode("ascii")

    def encrypt_bytes(self, payload: bytes) -> bytes:
        """Encrypt arbitrary bytes, for a value that is not UTF-8 text.

        The only caller is the document store (gap 12): a contract PDF is stored
        sealed by the same key ring as a field value, because a document holds
        every field on the page at once and writing one in the clear would undo
        gap 20's whole argument. Bytes in, bytes out — a PDF does not encode.

        Same envelope and same timestamped token as :meth:`encrypt`, so a sealed
        document answers :func:`is_sealed` the same way and is rewrapped by the
        same rotation.
        """
        return self._fernet.encrypt(payload)

    def decrypt(self, token: str) -> str:
        """Recover the plaintext from a token produced by :meth:`encrypt`.

        Tried against the current key first, then each retired key in the order
        they were listed, so a row written before a rotation stays readable for as
        long as the old key is carried.

        The exception deliberately carries no part of the token: these values are
        PII and an error string is the easiest way for PII to reach a log. It does
        name both variables, because "rotation is supported" is the thing an
        operator needs to know when this fires.
        """
        return self.decrypt_bytes(token.encode("ascii")).decode("utf-8")

    def decrypt_bytes(self, token: bytes) -> bytes:
        """Recover the bytes sealed by :meth:`encrypt_bytes`.

        The primitive, with :meth:`decrypt` the text-shaped wrapper over it, so
        there is one implementation of the key-ring walk rather than one per
        caller. Raises the same exception with the same message: the caller
        cannot tell, and does not need to, whether a failed unseal was a field
        value or a document.
        """
        plaintext = self._decrypt_with(self._fernet, token)
        if plaintext is not None:
            return plaintext
        retired_plaintext = self._from_retired(token)
        if retired_plaintext is not None:
            return retired_plaintext
        raise DecryptionError(
            "stored value could not be decrypted with ENCRYPTION_KEY or any "
            "ENCRYPTION_KEY_PREVIOUS entry"
        )

    def rewrap(self, token: str) -> str | None:
        """Re-seal a token under the current key, or ``None`` if it already is.

        This is the second half of a rotation: :meth:`decrypt` keeps old rows
        readable, and this makes them *stop* needing the old key. Returns ``None``
        rather than the same string so a caller cannot report a rewrite that did
        not happen, and raises :class:`DecryptionError` when no key on the ring
        opens the token — the same fail-closed answer :meth:`decrypt` gives, since
        re-wrapping a value nobody can read would only destroy it.

        Deliberately a failed-decrypt probe against the current key, which
        :func:`is_sealed` avoids: the question here is *which key* sealed this,
        not *whether* it is a token at all.
        """
        # Encoded once, because the private helpers below speak bytes: the ring
        # walk is shared with :meth:`decrypt_bytes`, and a sealed document goes
        # through the same path as a sealed field value.
        encoded = token.encode("ascii")
        if self._decrypt_with(self._fernet, encoded) is not None:
            return None
        plaintext = self._from_retired(encoded)
        if plaintext is None:
            # Names the variable that fixes it, for the same reason ``decrypt``
            # does: "the value cannot be read" is the symptom an operator already
            # has, and the missing piece is knowing that carrying the old key in
            # ENCRYPTION_KEY_PREVIOUS is what makes it readable again.
            raise DecryptionError(
                "stored value is sealed under a key this process does not hold; add "
                "that key to ENCRYPTION_KEY_PREVIOUS and re-run, otherwise the value "
                "cannot be re-sealed under ENCRYPTION_KEY"
            )
        return self.encrypt(plaintext.decode("utf-8"))

    def _from_retired(self, token: bytes) -> bytes | None:
        for retired in self._retired:
            plaintext = self._decrypt_with(retired, token)
            if plaintext is not None:
                return plaintext
        return None

    @staticmethod
    def _decrypt_with(fernet: Fernet, token: bytes) -> bytes | None:
        """``None`` on a wrong key, so the ring can try the next one."""
        try:
            return fernet.decrypt(token)
        except InvalidToken:
            return None


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

    ``ENCRYPTION_KEY_PREVIOUS`` is carried into the ring here and nowhere else,
    so every process that reads a stored value — the API's reveal route, the
    dispatcher, the scheduler, the CLI — inherits rotation support from one
    change rather than each remembering to pass it.
    """
    if not settings.encryption_key:
        raise MissingEncryptionKeyError(
            "ENCRYPTION_KEY is required to store extracted fields: raw values are "
            "encrypted at rest and there is no plaintext mode"
        )
    return FieldCipher(settings.encryption_key, settings.retired_encryption_keys)
