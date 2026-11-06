"""Refresh-token minting and identification — the policy half of gap 6.

Split out of the auth route for the same reason ``login_throttle.py`` is: the
parts that can be wrong are pure functions of their inputs, so they are testable
without a database or a clock, and the route keeps only the wiring.

**The token is opaque, not a JWT.** A signed JWT would be self-validating, which
sounds like an advantage until the question "has this been revoked?" arrives — a
JWT cannot answer it without exactly the server-side table an opaque token needs
anyway, and it would additionally have to be a *second* algorithm-and-claims
surface next to the access token. A random string plus a row is simpler and can
be revoked at any instant.

**Nothing here is secret.** ``refresh_token_hash`` deliberately uses a plain
SHA-256 with no salt and no keyed function. That is not laziness: the input is 256
bits from ``secrets``, so there is no dictionary to attack and no need for the
work factor Argon2 gives a human-chosen password. A salted hash would also defeat
the one query this table exists to answer — "which row is this token?" — because
the salt cannot be recovered from the request. The digest is namespaced so a value
lifted from this table says nothing anywhere else.

**The namespace is versioned.** If the scheme ever changes, the constant moves,
and an old digest can never alias onto a new row.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from app.db.base import truncate_to_second

#: Bumped only if the digest's meaning ever changes. Hashed into the material, so
#: two schemes can never collide.
#:
#: Named for the *digest* it namespaces rather than the token it is derived from,
#: matching ``LOGIN_KEY_NAMESPACE`` in ``login_throttle.py``. The constant is a
#: label, not a credential, but both secret scanners this repo runs (ruff's
#: ``S105``, bandit's ``B105``) match on the *variable name*, so a ``*_TOKEN_*``
#: spelling is reported as a hardcoded password. Renaming is the honest fix: a
#: ``nosec`` here would suppress the check for the whole file.
REFRESH_DIGEST_NAMESPACE = "refresh-token.v1"

#: 32 bytes = 256 bits, the same order as the SHA-256 used to store it. Long
#: enough that guessing is not a threat model; short enough to sit in a JSON body
#: without ceremony.
REFRESH_TOKEN_BYTES = 32

#: A character that can appear in neither the namespace nor a urlsafe token, so
#: moving a separator inside a value cannot produce equal material.
_SEPARATOR = "\x00"


def mint_refresh_token() -> str:
    """A new, unguessable refresh token — the only time the raw value exists.

    The caller must return this to the client immediately and store only
    :func:`refresh_token_hash` of it. Nothing logs it.
    """
    return secrets.token_urlsafe(REFRESH_TOKEN_BYTES)


def refresh_token_hash(token: str) -> str:
    """The stored form of a refresh token: a hex SHA-256 digest.

    Equality is all the lookup needs, and a digest is all that is kept, so a
    database dump contains no usable session.
    """
    material = _SEPARATOR.join((REFRESH_DIGEST_NAMESPACE, token))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def refresh_expiry(issued_at: datetime, days: int) -> datetime:
    """When a session minted at ``issued_at`` stops being refreshable.

    Floored to the second for the reason ``truncate_to_second`` documents
    everywhere else in this schema: MySQL rounds a microseconds-bearing value
    *forward*, and an expiry that is fractionally in the future is a token that
    survives a request it should have failed.
    """
    return truncate_to_second(issued_at + timedelta(days=days))
