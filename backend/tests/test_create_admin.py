"""The ``create-admin`` bootstrap command (gap 10).

Nothing else in the system can create the *first* user of a tenant:
``POST /auth/users`` is admin-only, and an admin token needs a user that does not
exist yet. Until this command existed the documented way out was a hand-written
REPL snippet in STATUS.md, so the first account of every deployment was made by
someone pasting Python that could drift from the models without anything noticing.

That makes the rules worth testing stricter than a normal provisioning path:

* **The password arrives on stdin or a prompt, never in argv**, and is bounded by
  the same constants as the API route rather than by a copy of them.
* **The slug is stored in the case the login form will look it up.** A tenant
  stored with a casing the form cannot reproduce is a tenant nobody can log into.
* **An existing account is a refusal, not a no-op and not a reset.** Reporting
  success would leave the operator believing they had just set that password, and
  resetting it would make the command a way to take over an admin account without
  knowing its password.
* **Every refusal leaves the database untouched**, which is why the checks all sit
  before the first flush — ``session_scope`` commits on a normal return.

The login test at the top goes through the real ASGI app on purpose. A hash that
``hash_password`` produced and nothing can verify is the failure mode this command
must not have, and only a real ``POST /auth/login`` proves it does not.
"""

from __future__ import annotations

import io
import sys
import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import cli as cli_module
from app.cli import EXIT_EXISTS, EXIT_FAILED, EXIT_OK
from app.cli import main as cli_main
from app.core.config import Settings
from app.core.security import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH
from app.db.enums import UserRole
from app.db.models import AuditLog, Tenant, User
from app.db.repositories.audit import ACTION_USER_CREATED, ACTOR_SYSTEM
from app.db.session import session_scope

LOGIN_PATH = "/api/v1/auth/login"

#: Not a credential: it protects in-memory databases that exist only for the
#: duration of a test, and it never leaves this process.
PASSWORD = "correct-horse-battery-staple"  # nosec B105
NEW_SLUG = "newco"
NEW_EMAIL = "ops@newco.test"


# --- helpers --------------------------------------------------------------


def tenant_by_slug(session_factory: sessionmaker[Session], slug: str) -> Tenant | None:
    with session_scope(session_factory) as session:
        return session.execute(select(Tenant).where(Tenant.slug == slug)).scalar_one_or_none()


def users_of(session_factory: sessionmaker[Session], tenant_id: uuid.UUID) -> list[User]:
    with session_scope(session_factory) as session:
        return list(session.execute(select(User).where(User.tenant_id == tenant_id)).scalars())


def audit_rows(session_factory: sessionmaker[Session]) -> list[AuditLog]:
    with session_scope(session_factory) as session:
        return list(session.execute(select(AuditLog)).scalars())


def login(client: TestClient, slug: str, email: str, password: str = PASSWORD) -> Response:
    """The real login route, which is the only proof the stored hash is usable."""
    return client.post(LOGIN_PATH, json={"tenant": slug, "email": email, "password": password})


@pytest.fixture
def run(
    session_factory: sessionmaker[Session],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., int]:
    """Invoke ``create-admin`` with the password on stdin, as a script would."""

    def _run(
        *args: str,
        password: str = PASSWORD,
        stdin_flag: str = "--password-stdin",
    ) -> int:
        monkeypatch.setattr(sys, "stdin", io.StringIO(f"{password}\n"))
        return cli_main(
            ["create-admin", stdin_flag, *args],
            settings=settings,
            session_factory=session_factory,
        )

    return _run


def argv(*, slug: str = NEW_SLUG, name: str | None = "New Co", email: str = NEW_EMAIL) -> list[str]:
    """The three arguments, in the order the parser declares them.

    Values are attached with ``=`` so a value that starts with a hyphen reaches the
    command's own validator instead of being consumed by argparse as another
    option — ``--tenant-slug -acme`` is an argparse error, ``--tenant-slug=-acme``
    is a slug the command gets to judge.
    """
    args = [f"--tenant-slug={slug}", f"--email={email}"]
    if name is not None:
        args += [f"--tenant-name={name}"]
    return args


# --- the happy path -------------------------------------------------------


def test_it_creates_the_tenant_and_an_active_admin(
    run: Callable[..., int], session_factory: sessionmaker[Session]
) -> None:
    code = run(*argv())

    assert code == EXIT_OK
    tenant = tenant_by_slug(session_factory, NEW_SLUG)
    assert tenant is not None
    assert tenant.name == "New Co"
    users = users_of(session_factory, tenant.id)
    assert len(users) == 1
    assert users[0].email == NEW_EMAIL
    assert users[0].role is UserRole.ADMIN
    assert users[0].is_active is True


def test_the_first_account_can_actually_log_in(run: Callable[..., int], client: TestClient) -> None:
    """The whole point of the command: a fresh database becomes reachable.

    This is the assertion the REPL snippet never had. It goes through the real
    route, so it covers the tenant lookup, the Argon2id verification and the token
    issue in one step — a hash nobody can verify would fail here and nowhere else.
    """
    assert run(*argv()) == EXIT_OK

    response = login(client, NEW_SLUG, NEW_EMAIL)

    assert response.status_code == 200, response.text
    assert response.json()["user"]["tenant_slug"] == NEW_SLUG
    assert response.json()["user"]["role"] == UserRole.ADMIN.value


def test_the_password_is_stored_only_as_an_argon2id_hash(
    run: Callable[..., int], session_factory: sessionmaker[Session]
) -> None:
    assert run(*argv()) == EXIT_OK

    tenant = tenant_by_slug(session_factory, NEW_SLUG)
    assert tenant is not None
    (user,) = users_of(session_factory, tenant.id)
    assert PASSWORD not in user.password_hash
    assert user.password_hash.startswith("$argon2id$")


def test_a_trailing_space_belongs_to_the_password(
    run: Callable[..., int], client: TestClient
) -> None:
    """``rstrip("\\r\\n")``, not ``strip()``.

    The newline a shell adds is not part of the password; a space the operator
    typed is. Trimming the latter would store a hash for a password that cannot be
    typed at the login form, and it would present as "wrong password".
    """
    spaced = f"{PASSWORD} "
    assert run(*argv(), password=spaced) == EXIT_OK

    assert login(client, NEW_SLUG, NEW_EMAIL, spaced).status_code == 200
    assert login(client, NEW_SLUG, NEW_EMAIL, PASSWORD).status_code == 401


def test_the_slug_is_stored_the_way_the_login_form_looks_it_up(
    run: Callable[..., int], client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    code = run(*argv(slug="Acme-Studio", name="Acme Studio", email="ops@acme.test"))

    assert code == EXIT_OK
    assert tenant_by_slug(session_factory, "acme-studio") is not None
    # Login lower-cases the slug too, so a mixed-case typing at the form works.
    assert login(client, "ACME-STUDIO", "ops@acme.test").status_code == 200


def test_an_existing_tenant_gets_a_second_admin(
    run: Callable[..., int],
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """Losing the only admin of a tenant is a real and recoverable state.

    The ``tenant`` fixture is ``acme``; no ``--tenant-name`` is passed because the
    name is only used when the tenant is being created.
    """
    code = run(*argv(slug=tenant.slug, name=None, email="second@acme.test"))

    assert code == EXIT_OK
    users = users_of(session_factory, tenant.id)
    assert [user.email for user in users] == ["second@acme.test"]
    assert tenant_by_slug(session_factory, tenant.slug) is not None


# --- refusals -------------------------------------------------------------


def test_a_duplicate_address_is_a_refusal_and_keeps_the_password(
    run: Callable[..., int],
    client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
) -> None:
    """``EXIT_EXISTS``, not success and not a reset.

    A quiet success would leave the operator believing they had just set that
    account's password; a reset would make this command a way to take over an
    admin account without knowing it. The original password must still work.
    """
    user = make_user(tenant, email="admin@acme.test")

    code = run(
        *argv(slug=tenant.slug, name=None, email=user.email), password="a-different-password"
    )

    assert code == EXIT_EXISTS
    assert len(users_of(session_factory, tenant.id)) == 1
    assert login(client, tenant.slug, user.email, PASSWORD).status_code == 200
    assert login(client, tenant.slug, user.email, "a-different-password").status_code == 401


def test_a_duplicate_refusal_writes_no_audit_row(
    run: Callable[..., int],
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
) -> None:
    """The log records what happened, so a run that changed nothing adds nothing."""
    user = make_user(tenant, email="admin@acme.test")

    assert run(*argv(slug=tenant.slug, name=None, email=user.email)) == EXIT_EXISTS
    assert audit_rows(session_factory) == []


def test_a_tenant_name_that_disagrees_with_the_stored_one_is_refused(
    run: Callable[..., int],
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    code = run(*argv(slug=tenant.slug, name="Something Else", email="ops@acme.test"))

    assert code == EXIT_FAILED
    assert users_of(session_factory, tenant.id) == []
    assert tenant.name == "Acme Ltd"


def test_a_new_slug_without_a_name_is_refused(
    run: Callable[..., int], session_factory: sessionmaker[Session]
) -> None:
    """A tenant with no display name would be created as ``None`` or ``""``."""
    code = run(*argv(slug=NEW_SLUG, name=None))

    assert code == EXIT_FAILED
    assert tenant_by_slug(session_factory, NEW_SLUG) is None


def test_an_inactive_tenant_is_refused(
    run: Callable[..., int],
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """An account in a deactivated tenant cannot log in, so creating one is a lie."""
    with session_scope(session_factory) as session:
        row = session.get(Tenant, tenant.id)
        assert row is not None
        row.is_active = False

    code = run(*argv(slug=tenant.slug, name=None, email="ops@acme.test"))

    assert code == EXIT_FAILED
    assert users_of(session_factory, tenant.id) == []


@pytest.mark.parametrize(
    "slug",
    ["", "   ", "-acme", "_acme", "acme ltd", "acme_ltd", "acmé", "a" * 65],
    ids=[
        "blank",
        "spaces",
        "leading-hyphen",
        "underscore",
        "inner-space",
        "trailing-word",
        "accent",
        "too-long",
    ],
)
def test_an_unusable_slug_is_refused(
    run: Callable[..., int], session_factory: sessionmaker[Session], slug: str
) -> None:
    """Rejected at creation, because a tenant nobody can log into is unrecoverable.

    The slug is half of a login credential, so its shape is checked here rather
    than discovered at the login form.
    """
    code = run(*argv(slug=slug))

    assert code == EXIT_FAILED
    assert tenant_by_slug(session_factory, slug.strip().lower()) is None


@pytest.mark.parametrize(
    "email",
    ["", "ops", "ops@", "@newco.test", "ops@newco", "two@at@newco.test", "ops @newco.test"],
    ids=["blank", "no-at", "no-domain", "no-local", "dotless-domain", "two-ats", "space"],
)
def test_an_unusable_email_is_refused(
    run: Callable[..., int], session_factory: sessionmaker[Session], email: str
) -> None:
    code = run(*argv(email=email))

    assert code == EXIT_FAILED
    # Refused before the tenant was created: the command does not leave behind the
    # half of a bootstrap that has no account in it.
    assert tenant_by_slug(session_factory, NEW_SLUG) is None


@pytest.mark.parametrize(
    "password",
    ["short", "x" * (MIN_PASSWORD_LENGTH - 1), "x" * (MAX_PASSWORD_LENGTH + 1)],
    ids=["way-too-short", "below-minimum", "above-maximum"],
)
def test_a_password_outside_the_api_bounds_is_refused(
    run: Callable[..., int], session_factory: sessionmaker[Session], password: str
) -> None:
    """The bounds are imported from ``app.core.security``, not retyped.

    A copy would drift from ``POST /auth/users`` and the two paths would disagree
    about what an acceptable password is.
    """
    code = run(*argv(), password=password)

    assert code == EXIT_FAILED
    assert tenant_by_slug(session_factory, NEW_SLUG) is None


def test_no_password_on_stdin_is_refused(
    run: Callable[..., int], session_factory: sessionmaker[Session]
) -> None:
    code = run(*argv(), password="")

    assert code == EXIT_FAILED
    assert tenant_by_slug(session_factory, NEW_SLUG) is None


# --- the prompt path ------------------------------------------------------


def test_a_mismatched_confirmation_is_refused_before_anything_is_written(
    session_factory: sessionmaker[Session],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bootstrap password cannot be read back, only re-typed wrongly.

    With one account on a fresh database, a mistyped password presents as
    "authentication is broken", so the confirmation is the only cheap guard.
    """
    answers = iter([PASSWORD, "a-different-password"])
    monkeypatch.setattr(cli_module.getpass, "getpass", lambda prompt="": next(answers))

    code = cli_main(["create-admin", *argv()], settings=settings, session_factory=session_factory)

    assert code == EXIT_FAILED
    assert tenant_by_slug(session_factory, NEW_SLUG) is None


def test_the_prompt_path_creates_the_account_when_both_answers_agree(
    client: TestClient,
    session_factory: sessionmaker[Session],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr(cli_module.getpass, "getpass", lambda prompt="": next(answers))

    code = cli_main(["create-admin", *argv()], settings=settings, session_factory=session_factory)

    assert code == EXIT_OK
    assert login(client, NEW_SLUG, NEW_EMAIL).status_code == 200


# --- the audit trail ------------------------------------------------------


def test_the_creation_is_audited_as_a_system_actor(
    run: Callable[..., int], session_factory: sessionmaker[Session]
) -> None:
    """The one account created with no principal behind it.

    There is no authenticated operator to attribute this to — that is precisely
    what "bootstrap" means — so the row says ``system`` with an empty ``actor_id``
    rather than borrowing a user id and fabricating a person.
    """
    assert run(*argv()) == EXIT_OK

    (row,) = audit_rows(session_factory)
    assert row.action == ACTION_USER_CREATED
    assert row.actor_type == ACTOR_SYSTEM
    assert row.actor_id is None
    assert row.entity == "users"
    assert row.correlation_id


def test_the_audit_row_names_the_role_and_carries_nothing_about_the_person(
    run: Callable[..., int], session_factory: sessionmaker[Session]
) -> None:
    """The audit log is readable by any operator, so it must not become a second
    copy of the credential material F7 and gap 20 went to the trouble of sealing."""
    assert run(*argv()) == EXIT_OK

    (row,) = audit_rows(session_factory)
    stored = f"{row.after_state or ''}{row.before_state or ''}{row.reason or ''}"
    assert UserRole.ADMIN.value in stored
    assert NEW_EMAIL not in stored
    assert PASSWORD not in stored
    assert "$argon2" not in stored


def test_the_audit_row_names_the_entity_that_was_created(
    run: Callable[..., int], session_factory: sessionmaker[Session]
) -> None:
    assert run(*argv()) == EXIT_OK

    tenant = tenant_by_slug(session_factory, NEW_SLUG)
    assert tenant is not None
    (user,) = users_of(session_factory, tenant.id)
    (row,) = audit_rows(session_factory)
    assert row.entity_id == str(user.id)
    assert row.tenant_id == tenant.id
