"""Login admission control — the policy half of gap 9.

``POST /auth/login`` verifies a password with Argon2id, which costs the *server*
tens of milliseconds per attempt. That is the right trade for making a stolen
hash expensive to crack, and exactly the wrong property to leave unbounded: an
attacker who may attempt indefinitely gets both an offline-speed guessing oracle
and a cheap way to burn the server's CPU. This module answers the one question
that stops it — *may this key try again yet?* — and nothing else.

The policy is split out as pure functions for the same reason
``workers/decision.py`` and ``workers/dispatch.py`` are: the interesting cases
(threshold boundaries, a window that has not yet lifted, an overrun from
concurrent failures) are then testable without a database, a clock, or an HTTP
client, and the route keeps only the wiring.

**What this defends and what it does not.** The key is the ``(tenant slug,
email)`` pair the caller submitted, so the control is per-account: it makes
guessing one account's password impractical and leaves the adjacent attack —
spraying one password across many accounts — to the edge, where admission
control belongs and where a client address can be judged honestly. Deriving a
key from ``X-Forwarded-For`` was rejected deliberately: the header is
caller-controlled unless a trusted proxy strips it, so keying on it would let an
attacker mint a fresh, empty counter with every request and defeat the throttle
entirely. A control that can be turned off by the attacker is worse than none,
because it reads as protection.

**Why refusing early leaks nothing.** The check runs *before* the password is
verified, which makes a throttled request measurably faster than an unthrottled
one. That is not an oracle: the decision depends only on values the caller
supplied, so a real account and a fictional one reach the same answer at the
same speed. The user-enumeration defence ``spend_password_hash_time`` exists for
is unaffected — it defends the *credential* check, which a throttled caller
never reaches.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.db.repositories.users import normalize_email

#: Bumped only if the digest's meaning ever changes; it is hashed into the key
#: so an old and a new scheme can never alias onto the same counter.
LOGIN_KEY_NAMESPACE = "login-throttle.v1"

#: A character that cannot appear in a slug or an address, so the three parts of
#: the material cannot be made to collide by moving a separator inside a value.
_KEY_SEPARATOR = "\x00"


def login_key_hash(tenant_slug: str, email: str) -> str:
    """The throttle key for one login attempt, as a hex SHA-256 digest.

    Both parts are folded exactly the way the login lookup folds them
    (``normalize_email`` for the address, ``strip().lower()`` for the slug), so
    ``Ops@Acme.test`` and ``ops@acme.test `` cannot occupy separate counters and
    give an attacker twice the attempts against one account.

    Hashing rather than storing the pair is what keeps the failure log free of
    PII: equality is all the throttle needs, and a digest has no other use.
    """
    material = _KEY_SEPARATOR.join(
        (LOGIN_KEY_NAMESPACE, tenant_slug.strip().lower(), normalize_email(email))
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def window_start(now: datetime, window_seconds: int) -> datetime:
    """The oldest instant still inside the counting window."""
    return now - timedelta(seconds=window_seconds)


@dataclass(frozen=True, slots=True)
class ThrottleDecision:
    """Whether to admit an attempt, and what to tell the caller if not."""

    is_blocked: bool
    #: Seconds until the oldest counted failure ages out. ``0`` when admitted.
    retry_after_seconds: int
    #: Failures in the window, for logging. Never sent to the client: the number
    #: of failures on an account is a hint about whether the account is real.
    failures: int


def decide(
    *,
    failures: int,
    earliest: datetime | None,
    now: datetime,
    max_attempts: int,
    window_seconds: int,
) -> ThrottleDecision:
    """Decide whether this attempt proceeds.

    ``failures`` is counted *before* the current attempt, so ``max_attempts=5``
    means "five failures are tolerated and the sixth attempt is refused" — which
    is how an operator reading the setting will expect it to behave.

    ``retry_after_seconds`` is the time until the block can lift, not a promise
    that it will: it is derived from the oldest failure in the window, so if
    concurrent attempts pushed the count beyond the threshold the reply is still
    honest (the count does drop) but the caller may be refused once more, with a
    fresh and smaller value. ``Retry-After`` is advisory by definition, and
    inventing a precise answer here would need a second query to find the
    ``failures - max_attempts + 1``-th oldest row.
    """
    if failures < max_attempts:
        return ThrottleDecision(is_blocked=False, retry_after_seconds=0, failures=failures)

    # ``max(1, ...)``: a ``Retry-After: 0`` invites an immediate retry loop and
    # is never what the limiter means.
    seconds = 1
    if earliest is not None:
        lift_at = earliest + timedelta(seconds=window_seconds)
        seconds = max(1, math.ceil((lift_at - now).total_seconds()))
    return ThrottleDecision(is_blocked=True, retry_after_seconds=seconds, failures=failures)
