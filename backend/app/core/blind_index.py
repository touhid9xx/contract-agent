"""Keyed, deterministic contact lookup — the blind index.

``ExtractedField.normalized_value`` is ciphertext (gap 20, D10-11), and that is the
point: a stolen table yields no email addresses. The cost is that "which contract
holds this phone number" stopped being a query. Fernet uses a fresh random IV per
call, so the same number seals to a different ciphertext every time and equality
over ciphertext matches nothing. :func:`app.workers.extraction.normalize_value` said
so in as many words: *"a blind index is what to add if something does."*

Something now does. A customer who replies STOP to a reminder text has to be matched
back to the contracts that texted them, and the inbound webhook knows three things:
a signature, a phone number, and the word STOP. It carries no tenant and no contract
id, so the number is the only handle there is.

The construction is HMAC-SHA256 under a **separate** key:

- **Keyed, not a plain digest.** A phone number is drawn from a space of roughly
  10^10 plausible values, so an unkeyed SHA-256 over it is reversible by enumeration
  in minutes — the column would be a phone directory written in hex. The key is the
  only thing that makes the column useless without it.
- **Deterministic.** That is the entire feature: equal numbers must produce equal
  digests or nothing can ever be matched.
- **Domain-separated.** The message is prefixed with a version string, so this use
  of the key cannot collide with any other, and a future change of construction can
  coexist with rows written by this one instead of silently failing to match them.

Three consequences, stated rather than left to be discovered:

1. **The key cannot be rotated in place.** Rotating it changes every digest, so
   every stored index stops matching. Unlike ``ENCRYPTION_KEY`` there is no key ring
   here — re-keying means recomputing the column, and that requires the *decryption*
   key for every contact field. It is a migration, not a config change, which is why
   ``CONTACT_BLIND_INDEX_KEY`` must be treated as long-lived and why it is not
   derived from either of the other secrets: rotating a token-signing key or a
   sealing key must not quietly destroy the lookup table for opt-outs.
2. **It is a correlation handle, not a secret store.** Anyone holding both the key
   and the database can test a guessed number against the index. That is the exact
   trade a lookup requires; keeping the key separate from the encryption key is what
   makes leaking one an incident rather than a catastrophe.
3. **Only contact fields are indexed.** A date has no inbound reply. Indexing every
   field would widen the guessable surface without serving a single query, so the
   decision of *which* fields carry an index lives in one place
   (:func:`contact_digest`) rather than at each call site.
"""

from __future__ import annotations

import hashlib
import hmac

from app.core.config import MIN_BLIND_INDEX_KEY_LENGTH, Settings
from app.db.enums import FieldName

__all__ = [
    "CONTACT_FIELD_NAMES",
    "ContactBlindIndex",
    "MissingBlindIndexKeyError",
    "build_blind_index",
    "contact_digest",
    "is_contact_field",
]

#: The fields whose value can arrive from the outside world and therefore needs to be
#: findable without decrypting the table. Deliberately a frozen set of the two
#: contact kinds: adding a third is a decision about what a stranger may match on.
CONTACT_FIELD_NAMES = frozenset({FieldName.EMAIL, FieldName.PHONE})

#: Version byte / namespace for the digest input. Changing the construction means a
#: new prefix, so rows written under this one stay matchable by this one.
_DOMAIN = b"contact-blind-index-v1\x00"

#: ``sha256`` is 32 bytes; hex is 64 characters, which is the column width.
DIGEST_LENGTH = 64


class MissingBlindIndexKeyError(RuntimeError):
    """Raised when an index is required but ``CONTACT_BLIND_INDEX_KEY`` is unset.

    A distinct type rather than a bare ``RuntimeError`` so the inbound route can turn
    it into a specific 503, the way :class:`app.core.crypto.MissingEncryptionKeyError`
    is handled: "the deployment is misconfigured" and "the request was bad" are
    different answers, and the second would be a lie.
    """


class ContactBlindIndex:
    """Computes the deterministic digest stored in ``ExtractedField.blind_index``.

    Holds key material, so it never renders it: :meth:`__repr__` is redacted for the
    same reason :class:`app.core.crypto.FieldCipher` redacts — these objects end up in
    tracebacks and debugger output, and a leak there is indistinguishable from a leak
    in the database.
    """

    __slots__ = ("_key",)

    def __init__(self, key: str) -> None:
        material = key.encode("utf-8")
        if len(material) < MIN_BLIND_INDEX_KEY_LENGTH:
            raise ValueError(
                f"the blind index key must be at least {MIN_BLIND_INDEX_KEY_LENGTH} bytes"
            )
        self._key = material

    def digest(self, normalized: str) -> str:
        """Return the hex digest of an **already normalized** contact value.

        ``normalized`` must be the output of
        :func:`app.workers.extraction.normalize_value`, not a raw value from a
        document: the digest is only worth anything if both sides of a future lookup
        passed through the same canonicalization. A raw ``"+1 (202) 555-0123"`` and a
        normalized ``"+12025550123"`` are different strings and would never match.

        Raises ``ValueError`` on a blank value: an empty digest would be a constant,
        and a constant indexed column matches every row that lacks a value.
        """
        if not normalized:
            raise ValueError("a blind index needs a value to hash")
        message = _DOMAIN + normalized.encode("utf-8")
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    def __repr__(self) -> str:
        return "ContactBlindIndex(<redacted>)"


def contact_digest(
    index: ContactBlindIndex, field_name: FieldName, normalized: str | None
) -> str | None:
    """The digest to store for one field, or ``None`` when the field is not a contact.

    This is the single place that decides which fields are findable. Both writers of
    ``ExtractedField`` call it — :func:`app.workers.extraction.extract_contract` on
    its create and overwrite branches, and ``_apply_correction`` in
    ``app/api/v1/review.py`` — so a reviewer's correction and a re-extraction produce
    the same index for the same value. Two implementations of this rule would be two
    ways for a corrected number to become unfindable.
    """
    if normalized is None or not is_contact_field(field_name):
        return None
    return index.digest(normalized)


def is_contact_field(field_name: FieldName) -> bool:
    """Whether a field's value is one a stranger could send back to us.

    Exposed so a caller that must *build* an index can ask before requiring its key,
    instead of demanding one for every field. Correcting a date needs no lookup: no
    inbound message will ever carry one. The answer still comes from
    ``CONTACT_FIELD_NAMES``, so there is one definition of the set.
    """
    return field_name in CONTACT_FIELD_NAMES


def build_blind_index(settings: Settings) -> ContactBlindIndex:
    """Build the index from settings, or refuse.

    Fail-closed, like :func:`app.core.crypto.build_cipher`: returning a no-op index
    would let writes silently skip the column, and a contact field with no index is
    invisible to the opt-out path — a customer's STOP would be accepted by the gateway
    and quietly do nothing.
    """
    key = settings.contact_blind_index_key
    if not key:
        raise MissingBlindIndexKeyError(
            "CONTACT_BLIND_INDEX_KEY is not set, so contact values cannot be matched "
            "against inbound replies"
        )
    return ContactBlindIndex(key)
