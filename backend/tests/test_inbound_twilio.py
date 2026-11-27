"""Inbound STOPs arriving from Twilio instead of the gateway.

The same route, a different arithmetic. :mod:`tests.test_inbound` pins the gateway's
scheme — ``X-Signature: hex(HMAC-SHA256(secret, body))``, verified over the bytes before
they are parsed. Twilio cannot be checked that way: it signs the **URL plus its sorted
form fields** with the **account auth token**, and base64-encodes an HMAC-SHA1. That
difference is not cosmetic, and most of this file exists because of it:

* the request has to be parsed *before* it can be verified, so the size gate upstream is
  what keeps an unverified parse cheap rather than unbounded;
* the URL is part of the signed material, so it is a configured value rather than
  something read off the request — a signature that can name what it is checked against
  is not a signature;
* the two schemes use different keys, so neither may authenticate the other. Two tests
  below try exactly that in both directions.

What must not differ is the outcome: a STOP from either gateway must revoke consent on
every contract holding that number, and must answer the same uniform ``202`` so nobody
can probe for which numbers are on file.
"""

from __future__ import annotations

import base64
import hmac
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from hashlib import sha1, sha256
from typing import Any
from urllib.parse import urlencode

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.v1.inbound import (
    TWILIO_SIGNATURE_HEADER,
    twilio_signature_matches,
)
from app.core.blind_index import ContactBlindIndex
from app.core.config import Settings
from app.core.crypto import FieldCipher
from app.db.enums import ContractStatus, FieldName, FieldStatus
from app.db.models import AuditLog, Contract, ExtractedField
from app.db.models.tenant import Tenant
from app.db.repositories.audit import ACTION_CONSENT_OPTED_OUT
from app.db.repositories.contracts import apply_consent
from app.db.session import session_scope
from app.main import create_app
from app.workers.extraction import normalize_value

INBOUND_PATH = "/api/v1/inbound/sms"

#: The account auth token Twilio signs with. Fabricated, and deliberately unlike the
#: gateway secret below so a test that mixes them up fails instead of passing.
AUTH_TOKEN = "twilio-account-auth-token-for-tests"
#: A gateway secret, set only in the cross-authentication tests.
GATEWAY_SECRET = "inbound-test-webhook-secret-0123456789"
INDEX_KEY = "inbound-test-contact-index-key-0123"
FERNET_KEY = Fernet.generate_key().decode("ascii")
CIPHER = FieldCipher(FERNET_KEY)

#: The exact URL the operator configured in Twilio's console. Part of the signature.
WEBHOOK_URL = "https://reminders.example.test/api/v1/inbound/sms"

STORED_PHONE = "+442079460958"
TYPED_PHONE = "+44 20 7946 0958"


# --- fixtures -------------------------------------------------------------
@pytest.fixture
def twilio_settings(settings: Settings) -> Settings:
    """Settings where the route authenticates Twilio requests and nothing else.

    ``model_copy`` for the same reason the gateway suite uses it: the copy keeps the
    in-memory database and the lowered Argon2 cost, and skips the *outbound* start-up
    refusals, which a deployment that only listens must not need.
    """
    return settings.model_copy(
        update={
            "inbound_transport": "twilio",
            "twilio_auth_token": AUTH_TOKEN,
            "twilio_webhook_url": WEBHOOK_URL,
            "contact_blind_index_key": INDEX_KEY,
            "encryption_key": FERNET_KEY,
        }
    )


@pytest.fixture
def twilio_client(
    twilio_settings: Settings, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    """A client over the whole app, so routing and dependencies are real."""
    app = create_app(twilio_settings, session_factory=session_factory)
    with TestClient(app) as test_client:
        yield test_client


# --- helpers --------------------------------------------------------------
def twilio_signature(
    fields: dict[str, str],
    *,
    url: str = WEBHOOK_URL,
    token: str = AUTH_TOKEN,
) -> str:
    """Twilio's signature, computed the way its documentation specifies.

    Sorted keys, each key concatenated with its value, appended to the URL, HMAC-SHA1
    under the account token, base64. Written out here rather than imported so the test
    is an independent second implementation: importing the one under test would make a
    wrong implementation agree with itself.
    """
    material = url + "".join(f"{key}{value}" for key, value in sorted(fields.items()))
    digest = hmac.new(token.encode("utf-8"), material.encode("utf-8"), sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def make_contract(
    session_factory: sessionmaker[Session],
    owner: Tenant,
    *,
    consented: bool = True,
) -> Contract:
    """One contract for ``owner``, consented by default.

    Consent is on by default here because the interesting claim is that a STOP
    *revokes* a live grant; a row whose flags were already false cannot show that.
    """
    with session_scope(session_factory) as session:
        row = Contract(
            tenant_id=owner.id,
            customer_ref="A. Rahman",
            timezone="UTC",
            status=ContractStatus.ACTIVE,
        )
        session.add(row)
        session.flush()
        if consented:
            apply_consent(
                row,
                consent_email=True,
                consent_sms=True,
                consent_voice=True,
                do_not_contact=False,
            )
        return row


def add_phone(
    session_factory: sessionmaker[Session],
    contract: Contract,
    number: str = STORED_PHONE,
) -> int:
    """Insert a sealed phone field the way extraction would, digest included."""
    canonical = normalize_value(FieldName.PHONE, number)
    assert canonical is not None
    digest = ContactBlindIndex(INDEX_KEY).digest(canonical)
    with session_scope(session_factory) as session:
        row = ExtractedField(
            tenant_id=contract.tenant_id,
            contract_id=contract.id,
            field_name=FieldName.PHONE,
            raw_value=CIPHER.encrypt(number),
            normalized_value=CIPHER.encrypt(canonical),
            blind_index=digest,
            confidence=0.95,
            status=FieldStatus.AUTO_ACCEPTED,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def fields(
    sender: str = STORED_PHONE,
    text: str = "STOP",
    sms_id: str = "SM1",
) -> dict[str, str]:
    """The form Twilio posts: ``From``/``Body``/``MessageSid`` plus envelope noise."""
    return {
        "AccountSid": "AC00000000000000000000000000000000",
        "From": sender,
        "To": "+18005551212",
        "Body": text,
        "MessageSid": sms_id,
    }


def post(
    client: TestClient,
    form: dict[str, str],
    *,
    signature: str | None = None,
    url: str = WEBHOOK_URL,
    **kwargs: Any,
) -> Any:
    """POST a Twilio form, signed unless ``signature`` says otherwise."""
    signed = twilio_signature(form, url=url) if signature is None else signature
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if signed:
        headers[TWILIO_SIGNATURE_HEADER] = signed
    return client.post(INBOUND_PATH, content=urlencode(form), headers=headers, **kwargs)


def consent_of(
    session_factory: sessionmaker[Session], contract: Contract
) -> tuple[bool, bool, bool, bool]:
    with session_scope(session_factory) as session:
        row = session.get(Contract, contract.id)
        assert row is not None
        return (row.consent_email, row.consent_sms, row.consent_voice, row.do_not_contact)


def opt_out_audits(session_factory: sessionmaker[Session]) -> list[AuditLog]:
    with session_scope(session_factory) as session:
        return list(
            session.scalars(select(AuditLog).where(AuditLog.action == ACTION_CONSENT_OPTED_OUT))
        )


# --- the signature itself -------------------------------------------------
def test_twilio_s_published_example_verifies() -> None:
    """The one vector Twilio prints in its own documentation.

    A golden borrowed from the vendor is worth more than one computed here: it fails if
    the scheme is misread in either direction, and it cannot drift with our code because
    the expected value came from outside the repository.

    The host is ``example.com`` because that is what the vendor's walk-through signs;
    the *other* example on the same page (``mycompany.com/myapp``) is the SDK snippet
    and carries its own signature, so mixing the two would produce a vector that no
    implementation can satisfy. This test failed for exactly that reason first.
    """
    published = {
        "CallSid": "CA1234567890ABCDE",
        "Caller": "+14158675310",
        "Digits": "1234",
        "From": "+14158675310",
        "To": "+18005551212",
    }

    assert (
        twilio_signature_matches(
            "12345",
            "https://example.com/myapp.php?foo=1&bar=2",
            published,
            "L/OH5YylLD5NRKLltdqwSvS0BnU=",
        )
        is True
    )


def test_a_tampered_field_does_not_verify() -> None:
    form = fields(text="STOP")
    signature = twilio_signature(form)

    assert (
        twilio_signature_matches(AUTH_TOKEN, WEBHOOK_URL, {**form, "Body": "unsub"}, signature)
        is False
    )


def test_the_same_fields_verify_under_a_different_gateway_token() -> None:
    """Key separation, stated as an executable claim rather than a comment."""
    form = fields()

    assert (
        twilio_signature_matches(
            "some-other-account-token", WEBHOOK_URL, form, twilio_signature(form)
        )
        is False
    )


def test_the_url_is_part_of_what_is_signed() -> None:
    """Why ``TWILIO_WEBHOOK_URL`` is configuration and not a guess from headers.

    If the URL were read off the request, a caller could pick the string its own
    signature is checked against. Pinning it means a signature minted for one URL cannot
    be replayed at another — including the same app reached by a different host name.
    """
    form = fields()
    signature = twilio_signature(form, url="https://evil.example.test/api/v1/inbound/sms")

    assert twilio_signature_matches(AUTH_TOKEN, WEBHOOK_URL, form, signature) is False


def test_an_unsorted_request_still_verifies() -> None:
    """Twilio sorts the fields before signing, so arrival order must not matter."""
    form = fields()
    signature = twilio_signature(dict(reversed(list(form.items()))))

    assert twilio_signature_matches(AUTH_TOKEN, WEBHOOK_URL, form, signature) is True


# --- the route ------------------------------------------------------------
def test_a_signed_stop_revokes_consent_through_twilio(
    twilio_client: TestClient, session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    contract = make_contract(session_factory, tenant)
    add_phone(session_factory, contract)
    assert consent_of(session_factory, contract)[1] is True

    response = post(twilio_client, fields(sender=TYPED_PHONE))

    # The same uniform 202 the gateway route returns: a different code for a known
    # number would let anyone enumerate which numbers are on file.
    assert response.status_code == 202
    assert consent_of(session_factory, contract) == (False, False, False, True)

    audits = opt_out_audits(session_factory)
    assert len(audits) == 1
    # Never the number that sent it: the audit snapshot stays free of contact data.
    # Both sides of the change, since a snapshot is taken before *and* after it.
    rendered = json.dumps({"before": audits[0].before_state, "after": audits[0].after_state})
    assert STORED_PHONE not in rendered
    assert TYPED_PHONE not in rendered


def test_a_stop_from_a_number_the_deployment_does_not_know_answers_the_same(
    twilio_client: TestClient, session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    contract = make_contract(session_factory, tenant)
    add_phone(session_factory, contract)

    response = post(twilio_client, fields(sender="+441234567890"))

    assert response.status_code == 202
    assert consent_of(session_factory, contract)[1] is True
    assert opt_out_audits(session_factory) == []


def test_a_message_that_is_not_a_stop_changes_nothing(
    twilio_client: TestClient, session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    contract = make_contract(session_factory, tenant)
    add_phone(session_factory, contract)

    response = post(twilio_client, fields(text="see you tuesday"))

    assert response.status_code == 202
    assert consent_of(session_factory, contract)[1] is True
    assert opt_out_audits(session_factory) == []


def test_a_second_stop_is_idempotent(
    twilio_client: TestClient, session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    contract = make_contract(session_factory, tenant)
    add_phone(session_factory, contract)

    assert post(twilio_client, fields()).status_code == 202
    assert post(twilio_client, fields(sms_id="SM2")).status_code == 202

    # One audit row, not two: the second STOP found nothing left to revoke.
    assert len(opt_out_audits(session_factory)) == 1


def test_a_missing_signature_is_refused(
    twilio_client: TestClient, session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    contract = make_contract(session_factory, tenant)
    add_phone(session_factory, contract)

    response = post(twilio_client, fields(), signature="")

    assert response.status_code == 401
    assert consent_of(session_factory, contract)[1] is True


def test_a_wrong_signature_is_refused(
    twilio_client: TestClient, session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    contract = make_contract(session_factory, tenant)
    add_phone(session_factory, contract)

    response = post(twilio_client, fields(), signature="bm90LWEtc2lnbmF0dXJl")

    assert response.status_code == 401
    assert consent_of(session_factory, contract)[1] is True


def test_a_gateway_signature_cannot_authenticate_the_twilio_route(
    twilio_client: TestClient, session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """Neither scheme may stand in for the other.

    A route that accepted "either" signature would be as strong as the weaker one, and
    the gateway's secret is the one an operator can choose and shorten.
    """
    contract = make_contract(session_factory, tenant)
    add_phone(session_factory, contract)
    raw = urlencode(fields()).encode("utf-8")
    gateway_signature = hmac.new(GATEWAY_SECRET.encode("utf-8"), raw, sha256).hexdigest()

    response = twilio_client.post(
        INBOUND_PATH,
        content=raw,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Signature": gateway_signature,
        },
    )

    assert response.status_code == 401
    assert consent_of(session_factory, contract)[1] is True


def test_the_route_is_absent_when_no_inbound_transport_is_configured(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """Unconfigured means not mounted, not "mounted and unauthenticated"."""
    app = create_app(
        settings.model_copy(update={"inbound_transport": "none"}),
        session_factory=session_factory,
    )

    with TestClient(app) as client:
        assert client.post(INBOUND_PATH, content=b"", headers={}).status_code == 404


# --- configuration --------------------------------------------------------
def _twilio_config(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "test",
        "log_json": False,
        "inbound_transport": "twilio",
        "twilio_auth_token": AUTH_TOKEN,
        "twilio_webhook_url": WEBHOOK_URL,
        "contact_blind_index_key": INDEX_KEY,
    }
    return Settings(**{**base, **overrides})


def test_twilio_inbound_without_an_auth_token_is_refused_at_start_up() -> None:
    """The token is the account auth token, not the API key secret used for sending.

    Stated in the refusal because the two are adjacent in the console and only one of
    them authenticates a webhook.
    """
    with pytest.raises(ValueError, match="TWILIO_AUTH_TOKEN"):
        _twilio_config(twilio_auth_token="")


def test_twilio_inbound_without_a_webhook_url_is_refused_at_start_up() -> None:
    with pytest.raises(ValueError, match="TWILIO_WEBHOOK_URL"):
        _twilio_config(twilio_webhook_url="")


def test_a_relative_webhook_url_is_refused() -> None:
    with pytest.raises(ValueError, match="TWILIO_WEBHOOK_URL"):
        _twilio_config(twilio_webhook_url="/api/v1/inbound/sms")


def test_a_plaintext_remote_webhook_url_is_refused() -> None:
    with pytest.raises(ValueError, match="must be https"):
        _twilio_config(twilio_webhook_url="http://reminders.example.test/api/v1/inbound/sms")


def test_a_loopback_webhook_url_may_stay_plaintext() -> None:
    """Local bring-up has to be possible without a tunnel; only the remote case is
    refused, and that is the case that carries a phone number over a network."""
    built = _twilio_config(twilio_webhook_url="http://127.0.0.1:8000/api/v1/inbound/sms")

    assert built.inbound_transport == "twilio"


def test_a_stale_gateway_secret_is_refused_rather_than_ignored() -> None:
    """A settings file that names the wrong authenticator is a configuration bug.

    Left alone it looks like the route is protected by a secret the code never reads,
    which is how a forged request gets investigated as an attack instead of corrected.
    """
    with pytest.raises(ValueError, match="INBOUND_WEBHOOK_SECRET"):
        _twilio_config(inbound_webhook_secret=GATEWAY_SECRET)


def test_twilio_inbound_still_needs_the_contact_index() -> None:
    """Listening is the capability that needs the index, not the vendor: without it a
    reply carries a number that no stored row can be found by, so every STOP would be
    accepted and honoured for nothing."""
    with pytest.raises(ValueError, match="CONTACT_BLIND_INDEX_KEY"):
        _twilio_config(contact_blind_index_key="")


def test_the_audit_clock_is_the_request_not_the_field(
    twilio_client: TestClient, session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """The opt-out is stamped when it happened, so a late-delivered webhook does not
    backdate the revocation to the field's creation."""
    contract = make_contract(session_factory, tenant)
    add_phone(session_factory, contract)
    before = datetime.now(UTC).replace(tzinfo=None)

    assert post(twilio_client, fields()).status_code == 202

    audit = opt_out_audits(session_factory)[0]
    stamped = audit.created_at
    if stamped.tzinfo is not None:
        stamped = stamped.astimezone(UTC).replace(tzinfo=None)
    assert stamped >= before
