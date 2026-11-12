"""The inbound SMS webhook: a customer withdrawing consent without our help.

Every other route in this application begins with a bearer token and a tenant read.
This one begins with a stranger's phone number, which is the whole point: an opt-out
that needs the customer to log in is not an opt-out. So the properties pinned here are
the ones that make an unauthenticated, cross-tenant, state-changing endpoint safe:

* **The signer is the gateway, and nothing else counts.** An unsigned request, a
  request signed with the wrong secret, and a request whose body was altered after
  signing all get ``401`` and change nothing. An unset secret fails closed rather than
  meaning "unsigned is fine".
* **The answer is the same whether or not the number is ours.** A matched number and an
  unknown one both get ``202 {"status": "accepted"}``. The oracle here is not the
  message but its *shape*: any difference would turn this route into a way to test
  whether a given phone number belongs to one of our tenants.
* **The number is looked up, never trusted for scoping.** A number that appears on
  contracts in two organisations suppresses both — the one place in this codebase where
  a read deliberately crosses tenants, because the customer cannot name one.
* **Delivering the same STOP twice is one act.** Replay safety comes from the *state*
  rather than from ``smsId``: ``apply_consent`` reports whether anything changed, and a
  second delivery that changes nothing writes no second audit row.
* **Only a bare keyword is an opt-out.** "stop by the office" is a person talking.

Not asserted here, because it is not true: that this has ever met a real gateway. Every
request below is built and signed in-process; see ``D10-93``'s limits.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Iterator

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.v1.inbound import SIGNATURE_HEADER
from app.core import metrics
from app.core.blind_index import ContactBlindIndex
from app.core.config import Settings
from app.core.crypto import FieldCipher
from app.db.enums import ContractStatus, FieldName, FieldStatus
from app.db.models import AuditLog, Contract, ExtractedField, Tenant
from app.db.repositories.audit import ACTION_CONSENT_OPTED_OUT, ACTOR_CUSTOMER
from app.db.repositories.contracts import apply_consent
from app.db.session import session_scope
from app.main import create_app
from app.workers.extraction import normalize_value

INBOUND_PATH = "/api/v1/inbound/sms"

#: Shared secret the fake gateway signs with. Protects in-memory state only.
SECRET = "inbound-test-webhook-secret-0123456789"
#: Key material for the contact index. Deliberately *not* the encryption key.
INDEX_KEY = "inbound-test-contact-index-key-0123"
#: Same key the tests' ``FieldCipher`` uses, so a sealed value is readable.
FERNET_KEY = Fernet.generate_key().decode("ascii")
CIPHER = FieldCipher(FERNET_KEY)

#: How extraction stores a number: canonical form, no spaces or punctuation.
STORED_PHONE = "+442079460958"
#: The same number as a person's phone would send it back, spaces and all.
TYPED_PHONE = "+44 20 7946 0958"


# --- fixtures -------------------------------------------------------------
@pytest.fixture
def inbound_settings(settings: Settings) -> Settings:
    """Settings with a gateway, a signing secret and a contact index.

    ``model_copy`` rather than a fresh ``Settings``: the copy keeps the in-memory
    database and lowered Argon2 cost the session already agreed on, and skips the
    gateways' *outbound* start-up refusals, which this route must not need — an
    organisation can well accept STOPs on a deployment that never sends SMS itself.
    """
    return settings.model_copy(
        update={
            "inbound_transport": "textbee",
            "inbound_webhook_secret": SECRET,
            "contact_blind_index_key": INDEX_KEY,
            "encryption_key": FERNET_KEY,
        }
    )


@pytest.fixture
def inbound_client(
    inbound_settings: Settings, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    """A client over the whole app, so routing and dependencies are real."""
    app = create_app(inbound_settings, session_factory=session_factory)
    with TestClient(app) as test_client:
        yield test_client


# --- helpers --------------------------------------------------------------
def make_contract(
    session_factory: sessionmaker[Session],
    owner: Tenant,
    *,
    consented: bool = False,
) -> Contract:
    """One contract for ``owner``; the row an opt-out has to reach.

    ``consented=True`` grants all three channels before the test runs, because a
    contract that starts with every flag already ``False`` cannot show that a STOP
    *revokes* a live grant — only that it flips a switch that was off anyway.
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
    *,
    indexed: bool = True,
) -> int:
    """Insert a sealed phone field the way extraction would, digest included.

    ``indexed=False`` reproduces a row written before the index existed, which is the
    only way to check that an unindexed row is *not* found — and therefore that the
    ``backfill-blind-index`` command is load-bearing rather than decorative.
    """
    canonical = normalize_value(FieldName.PHONE, number)
    assert canonical is not None
    digest = ContactBlindIndex(INDEX_KEY).digest(canonical) if indexed else None
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


def body(sender: str = STORED_PHONE, text: str = "STOP", sms_id: str = "sms-1") -> bytes:
    """A gateway payload, as the exact bytes the signature will cover."""
    return json.dumps(
        {"webhookEvent": "message", "sender": sender, "message": text, "smsId": sms_id}
    ).encode("utf-8")


def sign(payload: bytes, secret: str = SECRET) -> str:
    """The hex HMAC-SHA256 the gateway puts in ``X-Signature``."""
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def post(
    client: TestClient,
    payload: bytes,
    *,
    signature: str | bytes | None = None,
) -> httpx.Response:
    """Deliver a payload, signing it unless a caller passes a signature to forge.

    ``bytes`` as well as ``str``: a header value that is not ASCII has to be sent as
    raw bytes, because the client encodes ``str`` values as ASCII and raises for
    anything else — which is exactly the input a forged signature uses (see the
    non-ASCII test below).
    """
    if isinstance(signature, bytes):
        # httpx requires a header mapping whose keys and values are the same type, so
        # the str branch below cannot simply carry one odd value.
        raw_headers = {
            b"Content-Type": b"application/json",
            SIGNATURE_HEADER.encode("ascii"): signature,
        }
        return client.post(INBOUND_PATH, content=payload, headers=raw_headers)
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers[SIGNATURE_HEADER] = signature
    return client.post(INBOUND_PATH, content=payload, headers=headers)


def load_contract(session_factory: sessionmaker[Session], contract_id: int) -> Contract:
    with session_scope(session_factory) as session:
        row = session.get(Contract, contract_id)
        assert row is not None
        return row


def opt_out_audit_rows(session_factory: sessionmaker[Session]) -> list[AuditLog]:
    """Every opt-out audit row, whatever tenant wrote it."""
    with session_scope(session_factory) as session:
        stmt = select(AuditLog).where(AuditLog.action == ACTION_CONSENT_OPTED_OUT)
        return list(session.execute(stmt).scalars())


# --- the happy path -------------------------------------------------------
def test_a_signed_stop_withdraws_consent_and_leaves_an_audit_row(
    inbound_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """The one behaviour everything else exists to make safe."""
    contract = make_contract(session_factory, tenant)
    add_phone(session_factory, contract)

    response = post(inbound_client, body(), signature=sign(body()))

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    assert load_contract(session_factory, contract.id).do_not_contact is True

    rows = opt_out_audit_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].tenant_id == tenant.id
    # The customer, not an operator, and with no invented user id: they are not a
    # user of this deployment, and pretending otherwise would fabricate the link
    # between "consent withdrawn" and "a person we can name".
    assert rows[0].actor_type == ACTOR_CUSTOMER
    assert rows[0].actor_id is None


def test_the_audit_row_names_the_flags_and_never_the_number(
    inbound_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """An audit trail that leaks the phone number is a liability, not a record."""
    contract = make_contract(session_factory, tenant, consented=True)
    add_phone(session_factory, contract)

    post(inbound_client, body(), signature=sign(body()))

    row = opt_out_audit_rows(session_factory)[0]
    # ``AuditLogRepository`` stores each snapshot as a JSON *string* (``_snapshot``
    # sorts keys and serialises), so the two columns have to be parsed before they
    # can be read as mappings. The rendered form is kept for the leak assertions
    # below: what must not contain a phone number is the text actually persisted.
    assert row.before_state is not None
    assert row.after_state is not None
    before = json.loads(row.before_state)
    after = json.loads(row.after_state)
    # Every flag, not just the one that had to move. A snapshot naming only
    # ``do_not_contact`` would leave an auditor unable to tell whether the customer
    # said "no more SMS" or "no more anything".
    assert before == {
        "consent_email": True,
        "consent_sms": True,
        "consent_voice": True,
        "do_not_contact": False,
    }
    assert after == {
        "consent_email": False,
        "consent_sms": False,
        "consent_voice": False,
        "do_not_contact": True,
    }
    # The row is what the decision policy reads, so the revocation is also checked
    # where it takes effect rather than only in the log describing it.
    stored = load_contract(session_factory, contract.id)
    assert stored.consent_channels == set()
    assert stored.may_contact is False
    rendered = json.dumps({"before": row.before_state, "after": row.after_state})
    assert "7946" not in rendered
    assert STORED_PHONE not in rendered
    # The reason is the third column a caller could smuggle a value into, and it is
    # the one most likely to grow a "stop from +44..." one day.
    assert row.reason is not None
    assert "7946" not in row.reason


def test_a_number_typed_differently_matches_the_stored_canonical_form(
    inbound_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """The digest is over the *normalized* value on both sides, so formatting is free.

    Without this the opt-out only worked if the customer's phone echoed the number
    back exactly as extraction happened to store it.
    """
    contract = make_contract(session_factory, tenant)
    add_phone(session_factory, contract, STORED_PHONE)

    response = post(
        inbound_client, body(sender=TYPED_PHONE), signature=sign(body(sender=TYPED_PHONE))
    )

    assert response.status_code == 202
    assert load_contract(session_factory, contract.id).do_not_contact is True


def test_a_keyword_with_punctuation_and_mixed_case_is_honoured(
    inbound_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """A phone keyboard produces "Stop." — that is still the keyword."""
    contract = make_contract(session_factory, tenant)
    add_phone(session_factory, contract)
    payload = body(text="  Stop.  ")

    post(inbound_client, payload, signature=sign(payload))

    assert load_contract(session_factory, contract.id).do_not_contact is True


# --- the refusals ---------------------------------------------------------
def test_a_forged_signature_is_refused_and_changes_nothing(
    inbound_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """Signed with the wrong secret: the request never becomes a message."""
    contract = make_contract(session_factory, tenant)
    add_phone(session_factory, contract)
    payload = body()

    response = post(inbound_client, payload, signature=sign(payload, secret="wrong-secret"))

    assert response.status_code == 401
    assert load_contract(session_factory, contract.id).do_not_contact is False
    assert opt_out_audit_rows(session_factory) == []


def test_a_body_altered_after_signing_is_refused(
    inbound_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """The signature covers the bytes, so changing one byte invalidates it.

    This is the case a "hash the parsed fields" design would miss, and the reason the
    route reads the raw body before parsing anything.
    """
    contract = make_contract(session_factory, tenant)
    add_phone(session_factory, contract)
    original = body()
    tampered = body(text="STOP NOW")

    response = post(inbound_client, tampered, signature=sign(original))

    assert response.status_code == 401
    assert load_contract(session_factory, contract.id).do_not_contact is False


def test_a_delivery_with_no_signature_is_refused(
    inbound_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """An unsigned request is not "trusted by default" — it is a 401."""
    contract = make_contract(session_factory, tenant)
    add_phone(session_factory, contract)

    response = inbound_client.post(INBOUND_PATH, content=body())

    assert response.status_code == 401
    assert load_contract(session_factory, contract.id).do_not_contact is False


def test_a_signature_that_is_not_ascii_is_refused_rather_than_crashing(
    inbound_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """One high byte in the header must be a 401, not an unhandled 500.

    ``hmac.compare_digest`` raises ``TypeError`` when a ``str`` argument holds a
    non-ASCII character, and HTTP header values are latin-1 decoded — so comparing the
    two signatures as text made a forged header a cheap way for anyone, unauthenticated,
    to raise an exception inside the one route strangers can reach. Sent as bytes
    because the test client itself will not encode a non-ASCII header.
    """
    contract = make_contract(session_factory, tenant)
    add_phone(session_factory, contract)
    payload = body()
    forged = sign(payload).encode("ascii")[:63] + b"\xff"

    response = post(inbound_client, payload, signature=forged)

    assert response.status_code == 401
    assert load_contract(session_factory, contract.id).do_not_contact is False
    assert opt_out_audit_rows(session_factory) == []


def test_a_body_that_is_not_a_message_is_malformed(
    inbound_client: TestClient,
) -> None:
    """Signed but wrong shape: a 400 naming nothing about the failure's details."""
    payload = json.dumps({"webhookEvent": "message"}).encode("utf-8")

    response = post(inbound_client, payload, signature=sign(payload))

    assert response.status_code == 400


def test_a_sentence_that_merely_contains_stop_is_not_an_opt_out(
    inbound_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """ "stop by the office" is a person talking, and must not withdraw consent.

    A substring match would have turned an ordinary sentence into a suppression, and
    the customer would never learn why their reminders ended.
    """
    contract = make_contract(session_factory, tenant)
    add_phone(session_factory, contract)
    payload = body(text="please stop by the office on Friday")

    response = post(inbound_client, payload, signature=sign(payload))

    assert response.status_code == 202
    assert load_contract(session_factory, contract.id).do_not_contact is False
    assert opt_out_audit_rows(session_factory) == []


# --- no oracle ------------------------------------------------------------
def test_an_unknown_number_gets_the_same_answer_as_a_known_one(
    inbound_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """Identical status *and* body, or this route tells strangers who our customers are."""
    contract = make_contract(session_factory, tenant)
    add_phone(session_factory, contract)

    known = body(sender=STORED_PHONE, sms_id="sms-known")
    unknown = body(sender="+441619998888", sms_id="sms-unknown")

    first = post(inbound_client, known, signature=sign(known))
    second = post(inbound_client, unknown, signature=sign(unknown))

    assert first.status_code == second.status_code == 202
    assert first.json() == second.json() == {"status": "accepted"}
    # And the unmatched delivery wrote no row at all: there is nothing to write
    # about a number we do not hold.
    assert len(opt_out_audit_rows(session_factory)) == 1


def test_a_number_we_hold_but_never_indexed_is_not_found(
    inbound_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """Proof that ``backfill-blind-index`` is load-bearing, not decorative.

    A row written before the index existed has no digest, so a STOP for it matches
    nothing. The route still answers ``202`` — the customer is not told they were
    missed, which is why the operator has to be able to see the count.
    """
    contract = make_contract(session_factory, tenant)
    add_phone(session_factory, contract, indexed=False)

    response = post(inbound_client, body(), signature=sign(body()))

    assert response.status_code == 202
    assert load_contract(session_factory, contract.id).do_not_contact is False


# --- cross-tenant, replay, and the disabled route -------------------------
def test_a_number_on_two_tenants_is_suppressed_on_both(
    inbound_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    other_tenant: Tenant,
) -> None:
    """The customer cannot name a tenant, so the lookup crosses tenants by design.

    A shared office line, or one person who is a customer of two organisations:
    honouring the STOP for only one of them would keep texting them from the other.
    """
    first = make_contract(session_factory, tenant)
    second = make_contract(session_factory, other_tenant)
    add_phone(session_factory, first)
    add_phone(session_factory, second)

    post(inbound_client, body(), signature=sign(body()))

    assert load_contract(session_factory, first.id).do_not_contact is True
    assert load_contract(session_factory, second.id).do_not_contact is True
    # One audit row per organisation, each stamped with its own tenant: the widened
    # read did not widen the write.
    rows = opt_out_audit_rows(session_factory)
    assert {row.tenant_id for row in rows} == {tenant.id, other_tenant.id}
    assert len(rows) == 2


def test_the_second_delivery_of_the_same_stop_writes_nothing_new(
    inbound_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """Gateways retry when a 202 is slow. A retry is not a second opt-out.

    Note the second delivery carries a *different* ``smsId`` on purpose: replay
    safety here comes from the state, not from remembering ids, so a gateway that
    re-sends under a new id is still a no-op.
    """
    contract = make_contract(session_factory, tenant)
    add_phone(session_factory, contract)

    first = body(sms_id="sms-1")
    again = body(sms_id="sms-2")
    assert post(inbound_client, first, signature=sign(first)).status_code == 202
    assert post(inbound_client, again, signature=sign(again)).status_code == 202

    assert load_contract(session_factory, contract.id).do_not_contact is True
    assert len(opt_out_audit_rows(session_factory)) == 1


def test_without_a_gateway_the_endpoint_does_not_exist(
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    """``404``, not ``403``: a scanner learns nothing about a route that is switched off.

    The contact index is configured here deliberately. The route's dependency graph
    resolves ``blind_index_or_503`` *before* the handler runs, so a deployment missing
    the key would answer ``503`` and this test would be pinning the wrong refusal.
    That ordering is a real property of the route, recorded as a limit in ``D10-93``.
    """
    app = create_app(
        settings.model_copy(
            update={
                "inbound_transport": "none",
                "inbound_webhook_secret": SECRET,
                "contact_blind_index_key": INDEX_KEY,
            }
        ),
        session_factory=session_factory,
    )
    with TestClient(app) as test_client:
        response = post(test_client, body(), signature=sign(body()))

    assert response.status_code == 404


# --- the metric -----------------------------------------------------------
def _samples(text: str, outcome: str) -> float:
    """Read one labelled sample out of a Prometheus scrape, ``0`` when absent."""
    match = re.search(rf'sms_inbound_total\w*\{{outcome="{outcome}"\}} ([0-9.]+)', text)
    return float(match.group(1)) if match else 0.0


def test_the_unmatched_count_moves_and_the_opted_out_count_does_not(
    inbound_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """``unmatched`` is the number that matters: it is the consent teardown happening
    where nobody is watching. The opted-out count is a business metric; that one is
    not.

    Read as a delta, because the registry is process-wide and other tests in this
    file have already moved it.
    """
    before_unmatched = _samples(metrics.render(), "unmatched")
    before_opted_out = _samples(metrics.render(), "opted_out")
    contract = make_contract(session_factory, tenant)
    add_phone(session_factory, contract)

    known = body(sender=STORED_PHONE, sms_id="sms-metric-1")
    unknown = body(sender="+441619997777", sms_id="sms-metric-2")
    post(inbound_client, known, signature=sign(known))
    post(inbound_client, unknown, signature=sign(unknown))

    rendered = metrics.render()
    assert _samples(rendered, "unmatched") == before_unmatched + 1
    assert _samples(rendered, "opted_out") == before_opted_out + 1
