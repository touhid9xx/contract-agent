"""Authentication routes.

Ten endpoints, and the shape of each is a decision:

``POST /auth/login``
    Takes a tenant slug plus credentials and returns a short-lived access token
    plus a long-lived refresh token. The tenant is part of the request because
    login happens *before* a principal exists — nothing else in the system is
    allowed to take a tenant from the client. The token that comes back carries
    the id of the tenant that owned the matched user row, so a mis-targeted slug
    cannot mint a token for the wrong organisation.

``POST /auth/refresh``
    Trades a refresh token for a *new* access token and a *new* refresh token.
    Rotation, not reuse: the presented token is spent, and presenting it again is
    treated as theft. See :func:`refresh_session`.

``POST /auth/logout``
    Revokes the session a refresh token belongs to. Takes no access token, so a
    caller whose access token has already expired can still end their session.

``GET /auth/me``
    Echoes the resolved principal. Useful to a UI, and the smallest possible
    demonstration that the dependency chain works end to end.

``POST /auth/logout-all`` and ``POST /auth/password``
    The two self-service routes that end sessions the caller cannot name (gap 8).
    Both bump ``token_epoch`` *and* revoke the stored refresh rows, because either
    half alone leaves a working credential behind. ``/logout-all`` ends every
    session including this one; ``/password`` ends every *other* one and hands
    back a fresh pair so the caller stays signed in where they are.

``POST /auth/users``, ``GET /auth/users``, ``PATCH /auth/users/{id}`` and
``POST /auth/users/{id}/password-reset``
    Admin-only, and tenant-scoped by construction: the repository is built from
    ``principal.tenant_id``, so there is no request field that could aim the
    write at another organisation. The last two are the account-lifecycle half of
    gap 8 — a colleague moves, forgets their password, or leaves, and that must
    not require a database shell.

Every failure path is written to fail closed. There is no "if we cannot tell,
allow" branch anywhere in this module.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import (
    app_session_factory,
    app_settings,
    get_current_principal,
    get_request_session,
    require_roles,
)
from app.core.config import Settings
from app.core.login_throttle import (
    ThrottleDecision,
    decide,
    login_key_hash,
    window_start,
)
from app.core.middleware import correlation_id_or_new
from app.core.principal import Principal
from app.core.refresh import mint_refresh_token, refresh_expiry, refresh_token_hash
from app.core.security import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    generate_password,
    hash_password,
    issue_access_token,
    password_needs_rehash,
    spend_password_hash_time,
    verify_password,
)
from app.db.base import as_utc, truncate_to_second, utc_now
from app.db.enums import UserRole
from app.db.models import User
from app.db.repositories.audit import (
    ACTION_USER_CREDENTIAL_CHANGED,
    ACTION_USER_CREDENTIAL_RESET,
    ACTION_USER_UPDATED,
    ACTOR_USER,
    AUDIT_ENTITY_USER,
    AuditLogRepository,
)
from app.db.repositories.security import LoginAttemptRepository, RefreshTokenRepository
from app.db.repositories.users import (
    IdentityDirectory,
    UserRepository,
    is_plausible_email,
    normalize_email,
)
from app.db.session import session_scope

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

#: Login answers with one message for every cause — unknown tenant, unknown
#: user, wrong password, deactivated account. Anything more specific turns the
#: endpoint into an oracle for which addresses and organisations exist.
_INVALID_CREDENTIALS = "Invalid tenant, email or password"

#: Refusal for a login that has spent its attempt budget. A different status code
#: from the 401 above on purpose: a client cannot back off from a refusal it
#: cannot tell apart from "wrong password". It still says nothing about whether
#: the account exists, because the counter is keyed on the submitted pair alone
#: and an unknown address crosses the threshold identically.
_TOO_MANY_ATTEMPTS = "Too many failed login attempts; try again later"

#: One refusal for every cause a refresh can fail: unknown token, expired, already
#: spent, revoked, or an account that is no longer entitled to a session. A caller
#: holding a forged token and a caller holding a stolen-but-revoked one learn the
#: same thing, which is the only thing either should learn.
_REFRESH_REFUSED = "Invalid or expired refresh token"

#: Refusal for any authenticated route whose principal no longer resolves to a
#: row. It cannot normally happen — ``get_current_principal`` read that row a
#: moment earlier — so a hit means the account was deleted mid-request. Fail
#: closed, and never report success for a write that touched nothing.
_ACCOUNT_GONE = "Account is no longer available"

#: Refusal for an admin route aimed at an id that is not a colleague. Same answer
#: for "never existed" and "belongs to another organisation", so the route cannot
#: be used to probe which ids exist.
_NO_SUCH_USER = "No such user in this tenant"

#: Refusal for an administrator editing their own account through the admin
#: routes. Not tidiness: self-deactivation (or self-demotion from the last admin
#: seat) is exactly how a tenant loses its only administrator, and there is no
#: recovery path — nobody is left who can promote anyone back, and ``create-admin``
#: refuses a slug that already exists. A colleague is required, which incidentally
#: also means a second pair of eyes.
_SELF_CHANGE_REFUSED = (
    "An administrator cannot change their own role or active state; ask a colleague"
)

#: These three are named for the *credential* rather than the password they
#: mention, and the distinction is not cosmetic: ruff's ``S105`` and bandit's
#: ``B105`` match on the *variable name*, so a ``*_PASSWORD_*`` spelling is
#: reported as a hardcoded password in the source of a file whose whole job is to
#: talk about passwords. Renaming the identifier is the honest fix — a ``nosec``
#: would suppress the check for the file, including on a real secret. The values
#: users see still say "password", because that is the word the user typed.
#:
#: Refusal for ``POST /auth/password`` when the supplied current password is
#: wrong. Specific, unlike ``_INVALID_CREDENTIALS``: the caller is authenticated
#: and already knows their own account exists, so there is no account to hide and
#: a vague answer would only make a legitimate user retype a correct password.
_CURRENT_CREDENTIAL_WRONG = "Current password is incorrect"

#: Refusal for a password change that would set the password it is replacing.
#: Worth its own answer rather than a silent success: the change ends every other
#: session of the account, so "nothing changed" and "you are signed out
#: everywhere" must not be the same response.
_CREDENTIAL_REUSED = "New password must differ from the current one"

#: Separate from ``_TOO_MANY_ATTEMPTS`` because the abuse here is different: a
#: *stolen access token* being used to guess the password behind it, not a
#: credential stuffing attempt against ``/login``.
_TOO_MANY_CREDENTIAL_ATTEMPTS = "Too many incorrect password attempts; try again later"

#: Refusal for an admin ``PATCH`` carrying neither field. A no-op is refused
#: rather than answered with 200 so a client bug is visible at the call site
#: instead of looking like a write that landed.
_NOTHING_TO_CHANGE = "Provide at least one of role or is_active"

#: Refusal for a password reset aimed at a deactivated account. The reset would
#: store a hash nobody can use — login refuses an inactive user — so returning a
#: fresh temporary password would be a convincing lie. Reactivate first, then
#: reset; the two acts stay separately auditable.
_ACCOUNT_INACTIVE = "Account is deactivated; reactivate it before resetting the password"


class LoginRequest(BaseModel):
    """Credentials plus the tenant the caller intends to log in to."""

    tenant: str = Field(min_length=1, max_length=64, description="Tenant slug")
    email: str = Field(min_length=3, max_length=320)
    #: Bounded so a login attempt cannot hand a megabyte of text to the KDF.
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class CreateUserRequest(BaseModel):
    """An admin creating a colleague inside their own tenant."""

    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)
    role: UserRole = UserRole.VIEWER

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        if not is_plausible_email(value):
            raise ValueError("must be a valid email address")
        return normalize_email(value)


class UserSummary(BaseModel):
    """The user fields that are safe to return. Never the password hash.

    ``is_active`` is here because ``PATCH /auth/users/{id}`` is the one route that
    changes it: a response that omitted it would answer a deactivation and a
    reactivation with byte-identical bodies, leaving the caller to infer the
    outcome from a 200 it would have received either way.
    """

    id: uuid.UUID
    email: str
    role: UserRole
    tenant_id: uuid.UUID
    tenant_slug: str
    is_active: bool


class SessionResponse(BaseModel):
    """A freshly minted session: one access token and one refresh token.

    Returned by both ``/login`` and ``/refresh`` because the shape is genuinely
    the same — rotation hands back exactly what admission handed out the first
    time, which is what lets a client treat them with one code path.

    Both tokens sit in the body rather than the refresh token in an httpOnly
    cookie. That is a browser-safety compromise this milestone had to accept, and
    it is recorded as such in STATUS.md: the cookie is the right home for a token
    that must not be readable by script, but the SPA that would receive it does
    not exist yet (gap 7), and a cookie set here would be a CSRF surface with no
    client to opt into it.
    """

    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105  # a scheme name, not a secret
    expires_in: int
    expires_at: datetime
    #: The raw value exists only in this response. Only its SHA-256 is stored.
    refresh_token: str
    refresh_expires_at: datetime
    user: UserSummary


class RefreshRequest(BaseModel):
    """The token to redeem or revoke. Never logged, never echoed."""

    refresh_token: str = Field(min_length=1, max_length=512)


class ChangePasswordRequest(BaseModel):
    """A self-service password change: prove the old one, supply the new one.

    ``current_password`` is bounded like any other submitted password so the KDF
    cannot be handed a megabyte. ``new_password`` carries the same bounds the
    account was created under — a change must not be able to set a password that
    creation would have refused.
    """

    current_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)


class UpdateUserRequest(BaseModel):
    """A change to a colleague's role or active state. Both fields optional.

    Two nullable fields rather than separate routes: "deactivate" and "promote"
    are the same act on the same row and want the same guards, audit row and
    response. A request with neither set is refused rather than treated as a
    no-op, so a client bug is visible instead of silently succeeding.
    """

    role: UserRole | None = None
    is_active: bool | None = None


class PasswordResetResponse(BaseModel):
    """A reset password, visible exactly once.

    The field is in the response because there is nowhere else it can go: this
    milestone has no mail transport, so an administrator has to read the value to
    the user. It is returned once, never logged, never written to the audit log,
    and stored only as an Argon2id hash.
    """

    user: UserSummary
    temporary_password: str


def _user_summary(user: User, *, tenant_slug: str) -> UserSummary:
    return UserSummary(
        id=user.id,
        email=user.email,
        role=user.role,
        tenant_id=user.tenant_id,
        tenant_slug=tenant_slug,
        is_active=user.is_active,
    )


def _principal_summary(principal: Principal) -> UserSummary:
    """``/me``, from a token rather than a row.

    ``is_active`` is ``True`` by construction rather than by assumption:
    ``get_current_principal`` refuses an inactive user or tenant before any route
    body runs, so an authenticated caller is an active one. Reported anyway so the
    response shape does not change between ``/me`` and ``PATCH /auth/users/{id}``.
    """
    return UserSummary(
        id=principal.user_id,
        email=principal.email,
        role=principal.role,
        tenant_id=principal.tenant_id,
        tenant_slug=principal.tenant_slug,
        is_active=True,
    )


def _account_snapshot(user: User) -> dict[str, object]:
    """The account fields the audit log may hold. Never the password hash.

    ``token_epoch`` is in here on purpose: it is the observable consequence of a
    password change, a reset or a deactivation, so it is the value an operator
    lines a session's death up against. ``role`` and ``is_active`` say what
    entitlement changed. The hash says nothing an auditor may see and everything
    an attacker would want, so it is absent by construction rather than by
    redaction.
    """
    return {
        "role": user.role.value,
        "is_active": user.is_active,
        "token_epoch": user.token_epoch,
    }


def _audit_account_change(
    session: Session,
    *,
    principal: Principal,
    action: str,
    target: User,
    before: dict[str, object],
    after: dict[str, object] | None = None,
) -> None:
    """Append one audit row for a change to an account (gap 8).

    The tenant comes from the repository, never from the target row, so a row
    cannot be written onto another organisation's log. ``actor_id`` is always the
    principal — including on ``/password``, where actor and subject are the same
    person, because "this account's sessions were ended at this instant" is the
    fact worth keeping.
    """
    AuditLogRepository(session, principal.tenant_id).record(
        correlation_id=correlation_id_or_new(),
        actor_type=ACTOR_USER,
        actor_id=principal.user_id,
        action=action,
        entity=AUDIT_ENTITY_USER,
        entity_id=str(target.id),
        before=before,
        after=after,
    )


def _end_sessions(session: Session, *, user: User, now: datetime) -> int:
    """End every session of one account, both halves at once (gap 8).

    Two mechanisms exist and neither is sufficient alone, which is why they are
    welded together here rather than left to each caller:

    ``token_epoch`` is the half that reaches *access* tokens. A bearer token is
    verified without a database write, so a token already minted cannot be
    deleted — but it carries the epoch it was minted under, and
    ``get_current_principal`` refuses a mismatch. Bumping the column is therefore
    the only way to invalidate an access token before its fifteen minutes are up.
    It is also what makes a deactivation stick: reactivating the account does not
    restore an epoch it has moved past.

    Revoking the stored refresh rows is the half that reaches *refresh* tokens.
    Those are rows, so they could simply be deleted; they are stamped instead, so
    the log keeps the fact that the session existed. A stale-epoch row would be
    refused on redemption anyway, but leaving live rows behind means leaving a
    credential whose only guard is a check in another table.

    Returns how many live sessions the revocation ended, so a caller can report
    something true. The caller commits.
    """
    user.token_epoch += 1
    return RefreshTokenRepository(session).revoke_all_for_user(user.id, now=now)


def _record_failed_attempt(
    session_factory: sessionmaker[Session],
    *,
    key: str,
    settings: Settings,
    now: datetime,
) -> None:
    """Count one failed login, in a transaction of its own.

    It must not use the request session: that session is never committed (see
    ``get_request_session``), and the very ``HTTPException`` this is called
    before would roll an insert on it back. The counter would then never move and
    the throttle would be decorative — the one bug this whole slice exists to
    avoid, so the write is deliberately detached from the failed request.

    Best effort. If counting fails the caller still gets the correct 401: a wrong
    password must not be reported as a server error, and the honest cost of a
    lost row is that one attempt goes uncounted, not that a valid user is locked
    out or told something untrue.
    """
    try:
        with session_scope(session_factory) as scratch:
            LoginAttemptRepository(scratch).record(
                key,
                now=now,
                prune_before=window_start(now, settings.login_window_seconds),
            )
    except SQLAlchemyError:
        log.warning("login_attempt_not_recorded")


def _throttle_decision(
    session: Session, *, key: str, settings: Settings, now: datetime
) -> ThrottleDecision:
    """The admission decision for one throttle key (gap 9).

    Extracted so ``/login`` and ``/password`` ask *the same question in the same
    place*. An attacker holding a stolen access token must get exactly as many
    guesses at the password behind it as an anonymous caller gets at ``/login``,
    and two copies of this arithmetic would drift apart at the first tuning
    change.
    """
    failures, earliest = LoginAttemptRepository(session).window(
        key, since=window_start(now, settings.login_window_seconds)
    )
    return decide(
        failures=failures,
        earliest=earliest,
        now=now,
        max_attempts=settings.login_max_attempts,
        window_seconds=settings.login_window_seconds,
    )


def _refuse_refresh() -> HTTPException:
    """The single refusal every failed redemption returns.

    A 401 rather than a 403: the caller presented a credential and it bought
    nothing, which is what 401 means. The message is fixed, so a forged token, an
    expired one, a spent one and a revoked one are indistinguishable.
    """
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_REFRESH_REFUSED)


def _issue_session(
    session: Session,
    *,
    settings: Settings,
    user: User,
    tenant_slug: str,
    now: datetime,
    family_id: uuid.UUID | None = None,
    refresh_expires_at: datetime | None = None,
) -> SessionResponse:
    """Mint an access token and a stored refresh token for a verified user.

    Used by both ``/login`` and ``/refresh``, because a rotation hands back
    exactly what admission handed out the first time. The two callers differ in
    only two arguments, and both differences are the point:

    ``family_id``
        ``None`` at login — a new login is a new session family. On rotation it
        is the *existing* family, so a replay of any member can revoke every
        member, including the successor.

    ``refresh_expires_at``
        ``None`` at login, where the ceiling is
        ``REFRESH_TOKEN_EXPIRE_DAYS`` from now. On rotation it is the expiry of
        the token being replaced, so rotating never pushes the deadline out. The
        alternative — a fresh window per rotation — would make a session
        immortal for as long as the client keeps refreshing, and the configured
        lifetime would then bound nothing.

    The caller commits. Both routes do it at the same place, after the refresh
    row exists, so a commit failure can never return tokens the database cannot
    later honour.
    """
    access = issue_access_token(
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
        token_epoch=user.token_epoch,
        settings=settings,
    )
    raw_refresh = mint_refresh_token()
    expires_at = refresh_expires_at or refresh_expiry(now, settings.refresh_token_expire_days)
    RefreshTokenRepository(session).create(
        user_id=user.id,
        tenant_id=user.tenant_id,
        family_id=family_id or uuid.uuid4(),
        token_hash=refresh_token_hash(raw_refresh),
        token_epoch=user.token_epoch,
        issued_at=now,
        expires_at=expires_at,
    )
    return SessionResponse(
        access_token=access.token,
        expires_in=access.expires_in,
        expires_at=access.expires_at,
        refresh_token=raw_refresh,
        # Stored timestamps are naive UTC by convention (D2-01) while the JWT
        # expiry is already offset-aware. One response must not mix the two, so
        # the stored deadline is labelled on the way out.
        refresh_expires_at=as_utc(expires_at) or expires_at,
        user=_user_summary(user, tenant_slug=tenant_slug),
    )


def _clear_failed_attempts(session_factory: sessionmaker[Session], *, key: str) -> None:
    """Wipe the counter after a successful login, in its own transaction.

    Without this, a user who mistyped four times and then got in would start
    their next session one attempt away from a lockout. Detached from the request
    session for the same reason as ``_record_failed_attempt``, but best effort:
    failing to clear must never cost the caller a session they have earned.
    """
    try:
        with session_scope(session_factory) as scratch:
            LoginAttemptRepository(scratch).clear(key)
    except SQLAlchemyError:
        log.warning("login_attempts_not_cleared")


@router.post(
    "/login",
    response_model=SessionResponse,
    summary="Exchange credentials for an access token and a refresh token",
)
def login(
    payload: LoginRequest,
    request: Request,
    session: Session = Depends(get_request_session),
    session_factory: sessionmaker[Session] = Depends(app_session_factory),
) -> SessionResponse:
    """Authenticate a user of one tenant and mint a session.

    Every rejection below returns the same 401 with the same message, after
    spending the same Argon2id work, so neither the body nor the response time
    reveals whether the tenant or the address exists.

    Admission control runs first (`app.core.login_throttle`), before any password
    work, so a flood costs a digest and a count instead of an Argon2id
    verification. It is keyed on the submitted values alone and therefore adds no
    new oracle: a real account and a fictional one are refused at the same point,
    for the same reason, at the same speed.
    """
    settings = app_settings(request)
    directory = IdentityDirectory(session)

    now = utc_now()
    throttle_key = login_key_hash(payload.tenant, payload.email)
    throttle = _throttle_decision(session, key=throttle_key, settings=settings, now=now)
    if throttle.is_blocked:
        log.warning(
            "login_throttled",
            tenant=payload.tenant,
            failures=throttle.failures,
            retry_after_seconds=throttle.retry_after_seconds,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_TOO_MANY_ATTEMPTS,
            headers={"Retry-After": str(throttle.retry_after_seconds)},
        )

    tenant = directory.tenant_by_slug(payload.tenant)
    user = None
    if tenant is not None and tenant.is_active:
        user = UserRepository(session, tenant.id).by_email(payload.email)

    if user is None or tenant is None:
        spend_password_hash_time(settings)
        _record_failed_attempt(session_factory, key=throttle_key, settings=settings, now=now)
        log.info("login_failed", reason="no_matching_account", tenant=payload.tenant)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_INVALID_CREDENTIALS)

    # Always verify, even when the account is disabled: short-circuiting would
    # make "deactivated" measurably faster than "wrong password".
    password_ok = verify_password(payload.password, user.password_hash, settings)
    if not (password_ok and user.is_active):
        _record_failed_attempt(session_factory, key=throttle_key, settings=settings, now=now)
        log.info("login_failed", reason="bad_password_or_inactive_account")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_INVALID_CREDENTIALS)

    # Only when there is something to wipe, so the ordinary login — no failures in
    # the window — costs no extra transaction.
    if throttle.failures:
        _clear_failed_attempts(session_factory, key=throttle_key)

    # A successful login is the only moment the plaintext exists, so it is the
    # only place an old hash can be upgraded to the current cost. Best-effort: a
    # failure here must not deny a valid user their session.
    if password_needs_rehash(user.password_hash, settings):
        try:
            user.password_hash = hash_password(payload.password, settings)
            session.commit()
            log.info("password_rehashed", user_id=str(user.id))
        except SQLAlchemyError:
            session.rollback()
            log.warning("password_rehash_skipped", user_id=str(user.id))

    response = _issue_session(
        session,
        settings=settings,
        user=user,
        tenant_slug=tenant.slug,
        now=now,
    )
    # The refresh row is written by ``_issue_session`` and must be durable before
    # the tokens reach the client: a 200 carrying a refresh token that no row
    # backs would authenticate the caller for the access token's lifetime and
    # then fail every redemption. Committed once, here.
    session.commit()

    log.info("login_succeeded", user_id=str(user.id), tenant=tenant.slug)
    return response


@router.post(
    "/refresh",
    response_model=SessionResponse,
    summary="Rotate a refresh token for a new session",
)
def refresh_session(
    payload: RefreshRequest,
    request: Request,
    session: Session = Depends(get_request_session),
) -> SessionResponse:
    """Redeem a refresh token, spending it and returning a successor.

    The token is single-use. Redemption marks it spent and mints a replacement in
    the same family, so a client can keep a session alive indefinitely while any
    individual token it holds is worthless after one use.

    Presenting a spent token is treated as theft, not as a mistake: the whole
    family is revoked, which logs out the attacker *and* the legitimate client
    rather than leaving the attacker with the copy they rotated in. A client that
    raced itself across two tabs is caught by the same net and has to log in
    again — the deliberate cost of not being able to tell the two cases apart.
    That includes the race inside this function: the read below can see
    ``used_at`` as null a microsecond before another request spends the same
    token, which is why the authoritative gate is the conditional ``UPDATE`` in
    :meth:`RefreshTokenRepository.claim_for_rotation` and not the Python check.

    Entitlement is re-read from the database on every redemption — the row, its
    tenant, ``is_active`` on both, and ``token_epoch`` — because a refresh token
    that outlives a deactivated account would make deactivation meaningless. It
    is the same five-check posture ``get_current_principal`` takes, minus the
    bearer token: the refresh token *is* the credential here.
    """
    settings = app_settings(request)
    now = utc_now()
    digest = refresh_token_hash(payload.refresh_token)
    repository = RefreshTokenRepository(session)

    token = repository.by_hash(digest)
    if token is None:
        # No row means "never issued here" — a forged or already-pruned value.
        log.info("refresh_failed", reason="unknown_token")
        raise _refuse_refresh()

    if token.revoked_at is not None:
        # The family is already dead. Nothing left to detect, so nothing is
        # revoked here: the honest answer is that this token never had a chance.
        log.info("refresh_failed", reason="revoked")
        raise _refuse_refresh()

    if token.used_at is not None:
        repository.revoke_family(token.family_id, now=now)
        session.commit()
        log.warning("refresh_reuse_detected", family_id=str(token.family_id))
        raise _refuse_refresh()

    if token.expires_at <= truncate_to_second(now):
        log.info("refresh_failed", reason="expired")
        raise _refuse_refresh()

    user = IdentityDirectory(session).user_by_id(token.user_id)
    if user is None or user.tenant_id != token.tenant_id:
        # The row and the token disagree about who this is, or the account is
        # gone. Both are refusals, and neither may fall back to the token's claim.
        log.info("refresh_failed", reason="account_missing")
        raise _refuse_refresh()

    if not user.is_active or not user.tenant.is_active or user.token_epoch != token.token_epoch:
        # Deactivated user, deactivated organisation, or a bumped epoch: the
        # session was revoked by an act that happened after it was minted.
        log.info("refresh_failed", reason="not_entitled")
        raise _refuse_refresh()

    if not repository.claim_for_rotation(digest, now=now):
        # Lost the race to another request holding the same token. Indistinguish-
        # able from a replay, and answered the same way.
        repository.revoke_family(token.family_id, now=now)
        session.commit()
        log.warning("refresh_reuse_detected", family_id=str(token.family_id))
        raise _refuse_refresh()

    # ``token.expires_at`` is inherited rather than recomputed: rotation must not
    # extend the session past the ceiling the login set.
    response = _issue_session(
        session,
        settings=settings,
        user=user,
        tenant_slug=user.tenant.slug,
        now=now,
        family_id=token.family_id,
        refresh_expires_at=token.expires_at,
    )
    # One transaction: the claim above and the successor insert land together, so
    # there is no instant where the presented token is spent and no successor
    # exists — a crash between the two would otherwise end the session.
    session.commit()

    log.info("refresh_succeeded", user_id=str(user.id), family_id=str(token.family_id))
    return response


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Revoke the session a refresh token belongs to",
)
def logout(
    payload: RefreshRequest,
    request: Request,
    session: Session = Depends(get_request_session),
) -> None:
    """End the session, so its refresh token can never be redeemed again.

    Takes the refresh token rather than an access token, because the moment a
    caller most needs to log out is the moment their access token has expired.
    Requiring a valid one would leave a client holding a live refresh token and
    unable to use it or destroy it.

    Always 204, whether or not the token exists. A caller who could tell "revoked
    something" from "revoked nothing" would have a probe for which values were
    ever issued — and the holder of a token is already entitled to destroy it, so
    there is nothing to protect by being specific.
    """
    now = utc_now()
    token = RefreshTokenRepository(session).by_hash(refresh_token_hash(payload.refresh_token))
    if token is not None and token.revoked_at is None:
        RefreshTokenRepository(session).revoke_family(token.family_id, now=now)
        session.commit()
        log.info("logout", user_id=str(token.user_id), family_id=str(token.family_id))


@router.get("/me", response_model=UserSummary, summary="The authenticated principal")
def read_current_user(principal: Principal = Depends(get_current_principal)) -> UserSummary:
    """Return who the caller is, as resolved from their token and the database."""
    return _principal_summary(principal)


@router.get(
    "/users",
    response_model=list[UserSummary],
    summary="List the users of the caller's tenant",
)
def list_users(
    principal: Principal = Depends(require_roles(UserRole.ADMIN)),
    session: Session = Depends(get_request_session),
) -> list[UserSummary]:
    """Admin-only. The repository is scoped to the caller's tenant, so this can
    only ever return colleagues — never another organisation's users."""
    users = UserRepository(session, principal.tenant_id).list_all()
    return [_user_summary(user, tenant_slug=principal.tenant_slug) for user in users]


@router.post(
    "/users",
    response_model=UserSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user in the caller's tenant",
)
def create_user(
    payload: CreateUserRequest,
    request: Request,
    principal: Principal = Depends(require_roles(UserRole.ADMIN)),
    session: Session = Depends(get_request_session),
) -> UserSummary:
    """Admin-only. ``tenant_id`` comes from the principal, never from the body."""
    settings = app_settings(request)
    repository = UserRepository(session, principal.tenant_id)

    if repository.by_email(payload.email) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with that email already exists in this tenant",
        )

    user = repository.create(
        email=payload.email,
        password_hash=hash_password(payload.password, settings),
        role=payload.role,
    )
    try:
        session.commit()
    except SQLAlchemyError:
        # The pre-check above is advisory: two concurrent requests can both pass
        # it, and the unique constraint is what actually decides. Reporting the
        # constraint violation as 409 keeps the outcome stable instead of
        # surfacing a 500 the caller cannot act on.
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with that email already exists in this tenant",
        ) from None

    log.info("user_created", user_id=str(user.id), role=user.role.value)
    return _user_summary(user, tenant_slug=principal.tenant_slug)


@router.post(
    "/logout-all",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="End every session of the caller's account",
)
def logout_all(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_request_session),
) -> None:
    """Sign out everywhere, including here.

    ``/logout`` needs a refresh token because its whole point is to work when the
    access token is dead. This route is the other case: the caller has an access
    token, has decided it may have been seen, and wants the account to hold no
    live session at all. Requiring them to also produce the refresh token of the
    device they are worried about would make the remedy unavailable exactly when
    it is needed.

    No audit row, deliberately, matching ``/logout``: nothing about the account
    changed, and per-login events are the volume the audit log is meant to keep
    out. The epoch bump is still visible in the row itself for anyone lining a
    session's death up against it.
    """
    now = utc_now()
    user = UserRepository(session, principal.tenant_id).by_id(principal.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_ACCOUNT_GONE)

    ended = _end_sessions(session, user=user, now=now)
    session.commit()
    log.info("sessions_revoked", user_id=str(user.id), sessions=ended, cause="logout_all")


@router.post(
    "/password",
    response_model=SessionResponse,
    summary="Change the caller's password and end their other sessions",
)
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_request_session),
    session_factory: sessionmaker[Session] = Depends(app_session_factory),
) -> SessionResponse:
    """Replace the caller's password after proving the current one.

    Every other session dies. A password change happens because the old one may
    be known to someone else, so leaving the sessions it opened alive would
    secure the door and leave the windows open. The caller's own session is
    *rotated* rather than killed — they are demonstrably holding both the old
    password and a live token, and signing them out of the device they are
    sitting at would teach users to avoid the button.

    Rotation happens by ending everything and minting a fresh family, not by
    keeping the current one: reusing the family would mean the new refresh token
    answered to a family whose earlier members are still stamped ``used_at``, and
    a replay of any of them would then revoke the replacement too.

    Admission control is shared with ``/login`` — same key, same arithmetic, same
    window (``_throttle_decision``). A stolen access token is otherwise a free
    oracle for guessing the password behind it, which is how an attacker turns
    fifteen minutes into permanence.
    """
    settings = app_settings(request)
    now = utc_now()

    user = UserRepository(session, principal.tenant_id).by_id(principal.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_ACCOUNT_GONE)

    throttle_key = login_key_hash(principal.tenant_slug, principal.email)
    throttle = _throttle_decision(session, key=throttle_key, settings=settings, now=now)
    if throttle.is_blocked:
        log.warning(
            "password_change_throttled",
            user_id=str(user.id),
            failures=throttle.failures,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_TOO_MANY_CREDENTIAL_ATTEMPTS,
            headers={"Retry-After": str(throttle.retry_after_seconds)},
        )

    if not verify_password(payload.current_password, user.password_hash, settings):
        # Counted in its own transaction, like a failed login: this session is
        # never committed and the exception below would roll an insert back.
        _record_failed_attempt(session_factory, key=throttle_key, settings=settings, now=now)
        log.info("password_change_failed", user_id=str(user.id), reason="wrong_current_password")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=_CURRENT_CREDENTIAL_WRONG
        )

    if payload.new_password == payload.current_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_CREDENTIAL_REUSED)

    if throttle.failures:
        _clear_failed_attempts(session_factory, key=throttle_key)

    before = _account_snapshot(user)
    user.password_hash = hash_password(payload.new_password, settings)
    # Ends every session including the caller's, then the successor below is the
    # only live one. Order matters: revoking after issuing would stamp the new
    # row ``revoked_at`` and hand back a token that cannot be redeemed.
    ended = _end_sessions(session, user=user, now=now)
    _audit_account_change(
        session,
        principal=principal,
        action=ACTION_USER_CREDENTIAL_CHANGED,
        target=user,
        before=before,
        after=_account_snapshot(user),
    )
    response = _issue_session(
        session,
        settings=settings,
        user=user,
        tenant_slug=principal.tenant_slug,
        now=now,
    )
    session.commit()

    log.info("password_changed", user_id=str(user.id), sessions_ended=ended)
    return response


@router.patch(
    "/users/{user_id}",
    response_model=UserSummary,
    summary="Change a colleague's role or active state",
)
def update_user(
    user_id: uuid.UUID,
    payload: UpdateUserRequest,
    principal: Principal = Depends(require_roles(UserRole.ADMIN)),
    session: Session = Depends(get_request_session),
) -> UserSummary:
    """Admin-only account lifecycle: promote, demote, deactivate, reactivate.

    ``UserRepository`` is built from the principal's tenant, so ``user_id`` that
    belongs to another organisation resolves to ``None`` and answers 404 — the
    same answer as an id that never existed, which is what stops this route from
    being a directory probe.

    Any real change ends the target's sessions. A demotion has to, because the
    role travels *inside* the access token and ``require_roles`` reads it from
    there: without a bump, a demoted administrator keeps every permission they
    had for the rest of the token's life. A deactivation has to for the same
    reason plus one of its own — reactivating later must not resurrect a token
    minted before the account was switched off, and the epoch only moves forward.
    """
    if payload.role is None and payload.is_active is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_NOTHING_TO_CHANGE)

    target = UserRepository(session, principal.tenant_id).by_id(user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_SUCH_USER)
    if target.id == principal.user_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_SELF_CHANGE_REFUSED)

    before = _account_snapshot(target)

    changed = False
    if payload.role is not None and target.role != payload.role:
        target.role = payload.role
        changed = True
    if payload.is_active is not None and target.is_active != payload.is_active:
        target.is_active = payload.is_active
        changed = True

    if changed:
        _end_sessions(session, user=target, now=utc_now())
        _audit_account_change(
            session,
            principal=principal,
            action=ACTION_USER_UPDATED,
            target=target,
            before=before,
            after=_account_snapshot(target),
        )
        session.commit()
        log.info(
            "user_updated",
            user_id=str(target.id),
            role=target.role.value,
            is_active=target.is_active,
        )
    return _user_summary(target, tenant_slug=principal.tenant_slug)


@router.post(
    "/users/{user_id}/password-reset",
    response_model=PasswordResetResponse,
    summary="Reset a colleague's password and end their sessions",
)
def reset_user_password(
    user_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_roles(UserRole.ADMIN)),
    session: Session = Depends(get_request_session),
) -> PasswordResetResponse:
    """Admin-only. Generated password, returned once, never logged.

    The administrator does not choose the value: they would then know both halves
    of someone else's credential, and could use the account once the user had
    changed it. A generated password means the reset can only ever be a handover.

    Self-service is refused and so is a deactivated target. The first because
    ``/password`` is the path that proves the current password, and an admin
    resetting their own would skip that proof; the second because the response
    would carry a password that login is configured to refuse.

    The temporary password is not a field on the audit row, not in ``after``, and
    not in the log line below — the hash is all that is stored, and this response
    is the only copy that ever exists.
    """
    settings = app_settings(request)
    now = utc_now()

    target = UserRepository(session, principal.tenant_id).by_id(user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_SUCH_USER)
    if target.id == principal.user_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_SELF_CHANGE_REFUSED)
    if not target.is_active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_ACCOUNT_INACTIVE)

    before = _account_snapshot(target)
    temporary_password = generate_password()
    target.password_hash = hash_password(temporary_password, settings)
    ended = _end_sessions(session, user=target, now=now)
    _audit_account_change(
        session,
        principal=principal,
        action=ACTION_USER_CREDENTIAL_RESET,
        target=target,
        before=before,
        after=_account_snapshot(target),
    )
    session.commit()

    log.info("password_reset", user_id=str(target.id), sessions_ended=ended)
    return PasswordResetResponse(
        user=_user_summary(target, tenant_slug=principal.tenant_slug),
        temporary_password=temporary_password,
    )
