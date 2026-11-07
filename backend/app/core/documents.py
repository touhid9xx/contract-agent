"""Encrypted storage for the contract documents ingestion accepts (gap 12).

Until this module existed the system could only be told *about* a contract
(``POST /contracts`` takes metadata) and could never be handed one. That is why
``Contract.storage_path`` has existed in migration ``0001`` with no writer, and
why extraction has no first consumer: ``extract_contract`` takes a
``document_text`` that nothing in the system can produce.

Four properties, each one a decision rather than an accident:

**Content-addressed.** A document's key is the SHA-256 of its own *plaintext*, so
the same file stored twice is one blob, and the key doubles as the fingerprint
that ``contracts.sha256`` already exists to hold. Two tenants uploading one file
still get two contract rows (the uniqueness is ``(tenant_id, sha256)``) but share
the bytes, which is correct: the blob is the immutable input, the row is the
tenant's relationship to it.

**Sealed at rest.** A PDF holds every field on the page at once — name, address,
date of birth — so a document in the clear would undo gap 20's entire argument
about ``ExtractedField.raw_value``. Documents are sealed by the *same*
:class:`~app.core.crypto.FieldCipher` key ring as field values, which means one
``ENCRYPTION_KEY`` to configure and one rotation story rather than two. A store
with no key refuses to write; there is no plaintext mode (:func:`build_document_store`).

**Confined to its root.** A key read back from the database is untrusted input:
it is a string in a column, and a row edited by hand or a query that returns the
wrong thing must not be able to name ``../../etc/passwd``. Every key is checked
against a strict shape *and* resolved and re-checked inside the root before the
filesystem is touched (:meth:`DocumentStore._path_for`).

**Written before it is committed.** The route seals and writes the bytes, then
commits the row that names them. A crash between the two leaves an unreferenced
blob, which is harmless — content-addressed, deduplicated, and reachable again
whenever the same document is re-uploaded. The opposite order would leave a row
pointing at nothing, which is not recoverable by retrying.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings
from app.core.crypto import FieldCipher, build_cipher

__all__ = [
    "DocumentMissingError",
    "DocumentStore",
    "DocumentStoreError",
    "DocumentTooLargeError",
    "EmptyDocumentError",
    "InvalidDocumentKeyError",
    "StoredDocument",
    "build_document_store",
    "document_digest",
]

#: A stored key is ``<first two hex chars>/<full digest>``. The two-character
#: shard directory keeps any one directory to ~256 entries instead of one
#: directory holding every document the installation has ever ingested, which is
#: the difference between a listing call and a hang on some filesystems.
_KEY_RE = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{64}$")
#: Owner read/write only. The blobs are already ciphertext; this is the second
#: lock, so a misconfigured volume that exposes the directory to another local
#: account does not expose the documents.
_FILE_MODE = 0o600


class DocumentStoreError(RuntimeError):
    """Base for every refusal this module raises."""


class EmptyDocumentError(DocumentStoreError):
    """Raised when there are no bytes to store.

    Not a size violation: an empty document is a malformed request, not an
    oversized one, and the two deserve different answers.
    """


class DocumentTooLargeError(DocumentStoreError):
    """Raised when a document exceeds ``DOCUMENT_MAX_BYTES``.

    Carries both numbers because "too large" without the limit is a message the
    caller cannot act on.
    """

    def __init__(self, size: int, limit: int) -> None:
        super().__init__(f"document is {size} bytes, over the {limit}-byte limit")
        self.size = size
        self.limit = limit


class InvalidDocumentKeyError(DocumentStoreError):
    """Raised when a stored key is not one this store could have produced.

    Never includes the offending value in the message: a key is a path, and a
    path from a database row is exactly the kind of string that must not be
    echoed into a log or an API response.
    """


class DocumentMissingError(DocumentStoreError):
    """Raised when a key is well-formed but no blob exists under it.

    Distinct from :class:`InvalidDocumentKeyError` on purpose: this one means the
    row and the store disagree (a volume that was not mounted, a restored
    database, a pruned directory), which is an operator problem rather than a
    bug in the caller.
    """


@dataclass(frozen=True, slots=True)
class StoredDocument:
    """What :meth:`DocumentStore.save` reports about a stored blob."""

    #: SHA-256 of the plaintext, hex. This is what belongs in
    #: ``contracts.sha256`` — the same column metadata ingestion fills with a
    #: hash of the request, which is why the two schemes coexist in one column
    #: without ambiguity: both are SHA-256 of *something*, and the same document
    #: hashes identically every time.
    sha256: str
    #: Path relative to the store root, as persisted in ``contracts.storage_path``.
    #: Deliberately relative: the root differs between a development checkout and
    #: a container volume, and a row that recorded an absolute path would only
    #: resolve on the machine that wrote it.
    key: str
    #: Size of the plaintext in bytes. Reported so a caller can log it without
    #: decrypting, and because it is the number the cap is checked against.
    size: int


def document_digest(payload: bytes) -> str:
    """The SHA-256 of a document's bytes, hex-encoded.

    Exposed separately from :meth:`DocumentStore.save` because the caller needs
    it *before* storing: the idempotency check reads ``contracts.sha256`` first,
    and a duplicate should be refused without sealing a document nobody will
    reference.
    """
    return hashlib.sha256(payload).hexdigest()


class DocumentStore:
    """A directory of sealed documents, addressed by content.

    Bound to one root and one cipher. Instances are cheap and carry no state
    beyond those two, so a route can build one per request without a pool.
    """

    def __init__(self, root: str | Path, cipher: FieldCipher, *, max_bytes: int) -> None:
        # Resolved once, at construction: every later confinement check compares
        # against this, and comparing against an unresolved root would let a
        # relative ``DOCUMENT_STORE_PATH`` be defeated by a change of working
        # directory between two calls (config.py refuses a relative path outside
        # development for the same reason).
        self._root = Path(root).resolve()
        self._cipher = cipher
        self._max_bytes = max_bytes

    @property
    def root(self) -> Path:
        """The resolved directory documents live under."""
        return self._root

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    def save(self, payload: bytes) -> StoredDocument:
        """Seal ``payload`` and write it, or recognise it as already stored.

        Idempotent by construction: the destination is derived from the content,
        so a second upload of the same document finds the blob already in place
        and returns without rewriting it. That also means a re-saved document is
        never re-sealed under a different key by accident — the bytes on disk stay
        as they were until an operator rotates deliberately.
        """
        if not payload:
            raise EmptyDocumentError(
                "a document must contain at least one byte; an empty upload is a "
                "malformed request rather than an empty contract"
            )
        size = len(payload)
        if size > self._max_bytes:
            raise DocumentTooLargeError(size, self._max_bytes)

        digest = document_digest(payload)
        key = f"{digest[:2]}/{digest}"
        path = self._path_for(key)
        if path.exists():
            return StoredDocument(sha256=digest, key=key, size=size)

        sealed = self._cipher.encrypt_bytes(payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_atomic(path, sealed)
        return StoredDocument(sha256=digest, key=key, size=size)

    def load(self, key: str) -> bytes:
        """Return the plaintext of a stored document.

        Fails closed in three distinct ways, because the operator response
        differs: an invalid key is a bug or a tampered row, a missing blob is a
        storage/disagreement problem, and a failed decryption (raised by
        :class:`~app.core.crypto.FieldCipher`) is a key-ring problem.
        """
        path = self._path_for(key)
        try:
            sealed = path.read_bytes()
        except FileNotFoundError:
            raise DocumentMissingError(
                f"no document stored for key {key}; the store root is "
                f"{str(self._root)!r}, check the volume is mounted and populated"
            ) from None
        # Not wrapped: ``DecryptionError`` already names the variables that fix it
        # and deliberately carries no part of the token, which is the same
        # contract field values get.
        return self._cipher.decrypt_bytes(sealed)

    def has(self, key: str) -> bool:
        """Whether a well-formed key currently has bytes behind it."""
        return self._path_for(key).exists()

    def _path_for(self, key: str) -> Path:
        """Resolve a stored key to a path inside the root, or refuse.

        Two checks rather than one. The shape regex rejects ``..``, absolute
        paths, drive letters and anything that is not a key this store would have
        produced; the containment check then catches what a shape rule cannot —
        a symlink inside the shard directory pointing out of the root. Neither is
        sufficient alone, and the cost of both is a string match and one
        ``resolve``.
        """
        if not _KEY_RE.match(key):
            raise InvalidDocumentKeyError(
                "stored document key is not in the form '<two hex>/<sha256>'"
            )
        resolved = (self._root / key).resolve()
        if not resolved.is_relative_to(self._root):
            raise InvalidDocumentKeyError("stored document key resolves outside the store root")
        return resolved

    @staticmethod
    def _write_atomic(path: Path, sealed: bytes) -> None:
        """Write ``sealed`` so a reader never observes a partial file.

        ``os.replace`` within one directory is atomic, so the visible name only
        ever points at a complete blob. A crash mid-write leaves a ``.part`` file
        that nothing references, which is why the temporary file carries a prefix
        that distinguishes it from a key.
        """
        handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".part-")
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(sealed)
                stream.flush()
                # Durability before the rename: otherwise a host crash can leave
                # the final name pointing at truncated content, which would
                # decrypt to a failure rather than to the document.
                os.fsync(stream.fileno())
            os.chmod(temp_name, _FILE_MODE)
            os.replace(temp_name, path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise


def build_document_store(settings: Settings, *, cipher: FieldCipher | None = None) -> DocumentStore:
    """Build the store for this process, or refuse to.

    Goes through :func:`~app.core.crypto.build_cipher`, so the same fail-closed
    rule applies as for field values: a process with no ``ENCRYPTION_KEY`` cannot
    accept a document, and says so instead of writing one in the clear.

    ``cipher`` is injectable so a caller that already resolved one — the API route
    depends on ``cipher_or_503``, which is what turns a missing key into a 503
    rather than a 500 — does not build a second key ring to hand it back here.
    """
    resolved = cipher if cipher is not None else build_cipher(settings)
    return DocumentStore(
        settings.document_store_path,
        resolved,
        max_bytes=settings.document_max_bytes,
    )
