"""Channel selection for approved contracts (F9).

F8 made ``approved`` reachable and nothing consumed it (gap 15). This slice is the
consumer, so the tests are grouped around three claims:

* **The policy refuses more often than it decides.** Six of the tests below are
  refusal paths, because the interesting cases in a contact policy are the ones
  where the system declines — a closed contract, an unreviewed field, an opt-out,
  a consent nobody gave.
* **Consent cannot be widened by a request.** The only inputs are the contract row
  and its fields; there is no body field, query parameter or header that adds a
  channel, and the opt-out veto outranks a full grant.
* **Nothing readable is PII.** ``reasoning`` is plain text any operator can read, so
  the recurring negative assertion here is that no response and no stored decision
  contains the contact value that made the decision possible.

Consent arrives with this milestone too (the columns are new), so its own write path
is tested here rather than in the review suite: ``PUT /contracts/{id}/consent`` is
what makes any decision possible at all.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.enums import ContractStatus, FieldName, FieldStatus, UserRole
from app.db.models import AgentDecision, AuditLog, Contract, ExtractedField, Tenant, User
from app.db.session import session_scope
from app.workers.decision import CHANNEL_POLICY_VERSION, DETERMINISTIC_DECIDER

LOGIN_PATH = "/api/v1/auth/login"
DECISIONS_PATH = "/api/v1/agent/decisions"
CONSENT_PATH = "/api/v1/contracts/{contract_id}/consent"

#: Matches ``conftest.DEFAULT_PASSWORD``; protects in-memory databases only.
PASSWORD = "correct-horse-battery-staple"  # nosec B105
EMAIL = "ops@acme.test"

#: The values the policy must never disclose. Distinct per field so a leak names
#: which field leaked.
CONTACT_EMAIL = "a.rahman@example.test"
CONTACT_PHONE = "+442079460958"


# --- fixtures and helpers -------------------------------------------------
@pytest.fixture
def contract(session_factory: sessionmaker[Session], tenant: Tenant) -> Contract:
    """A live contract consenting to email only, with nothing extracted yet."""
    return make_contract(session_factory, tenant)


@pytest.fixture
def unconsented(session_factory: sessionmaker[Session], tenant: Tenant) -> Contract:
    """A live contract that has granted no channel — the state after ingestion.

    Kept separate from :func:`contract` because ``PUT /consent`` treats a write that
    changes nothing as a no-op: against the email-consenting fixture, "record email
    consent" would not be a change and the audit/version assertions would be vacuous.
    """
    return make_contract(session_factory, tenant, consent_email=False)


def make_contract(
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    *,
    status: ContractStatus = ContractStatus.ACTIVE,
    consent_email: bool = True,
    consent_sms: bool = False,
    consent_voice: bool = False,
    do_not_contact: bool = False,
) -> Contract:
    """Create one committed contract with explicit consent state.

    Consent is spelled out on every call rather than defaulted deep in a factory:
    which channels a case consents to is the whole point of most of these tests, and
    a hidden default would make two of them silently identical.
    """
    with session_scope(session_factory) as session:
        row = Contract(
            tenant_id=tenant.id,
            customer_ref="A. Rahman",
            end_date=date(2026, 1, 1),
            status=status,
            consent_email=consent_email,
            consent_sms=consent_sms,
            consent_voice=consent_voice,
            do_not_contact=do_not_contact,
        )
        session.add(row)
        session.flush()
        return row


def add_field(
    session_factory: sessionmaker[Session],
    contract: Contract,
    *,
    field_name: FieldName = FieldName.EMAIL,
    value: str | None = CONTACT_EMAIL,
    confidence: float = 0.93,
    status: FieldStatus = FieldStatus.APPROVED,
) -> int:
    """Store one extracted field and return its id.

    ``normalized_value`` is written in the clear here on purpose: encryption at rest
    is F7's guarantee and is proven in ``test_crypto``/``test_extraction``. This
    suite is about whether a *usable* value exists, and the policy reads the
    normalized column for exactly that.
    """
    with session_scope(session_factory) as session:
        row = ExtractedField(
            tenant_id=contract.tenant_id,
            contract_id=contract.id,
            field_name=field_name,
            raw_value=None,
            normalized_value=value,
            confidence=confidence,
            status=status,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def load_contract(session_factory: sessionmaker[Session], contract_id: int) -> Contract:
    """Re-read a contract through a fresh session, so assertions see stored state."""
    with session_scope(session_factory) as session:
        row = session.get(Contract, contract_id)
        assert row is not None
        return row


def decisions(session_factory: sessionmaker[Session]) -> list[AgentDecision]:
    with session_scope(session_factory) as session:
        return list(session.execute(select(AgentDecision).order_by(AgentDecision.id)).scalars())


def audit_rows(session_factory: sessionmaker[Session]) -> list[AuditLog]:
    with session_scope(session_factory) as session:
        return list(session.execute(select(AuditLog).order_by(AuditLog.id)).scalars())


def login(client: TestClient, tenant: Tenant, email: str = EMAIL) -> dict[str, str]:
    response = client.post(
        LOGIN_PATH,
        json={"tenant": tenant.slug, "email": email, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def auth(
    client: TestClient,
    tenant: Tenant,
    make_user: Callable[..., User],
    *,
    role: UserRole = UserRole.OPERATOR,
    email: str = EMAIL,
) -> dict[str, str]:
    make_user(tenant, email=email, role=role)
    return login(client, tenant, email)


def decide(client: TestClient, contract_id: int, headers: dict[str, str]) -> Any:
    return client.post(DECISIONS_PATH, json={"contract_id": contract_id}, headers=headers)


def refusal_code(response: Any) -> str:
    """The machine-readable reason from a 409, as a caller would read it."""
    return str(response.json()["error"]["code"])


# --- the happy path -------------------------------------------------------
def test_an_approved_email_on_a_consented_contract_is_emailed(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract)
    headers = auth(client, tenant, make_user)

    response = decide(client, contract.id, headers)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["channel"] == "email"
    assert body["contract_id"] == contract.id
    # The confidence that matters is the contact field's, not the contract's risk
    # and not some average over unrelated fields.
    assert body["confidence"] == pytest.approx(0.93)
    assert body["needs_review"] is False
    assert body["prompt_version"] == CHANNEL_POLICY_VERSION
    # Not a model name: no model was consulted (D9-02).
    assert body["llm_model"] == DETERMINISTIC_DECIDER
    assert body["correlation_id"]


def test_the_decision_is_persisted(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract)
    headers = auth(client, tenant, make_user)

    decide(client, contract.id, headers)

    stored = decisions(session_factory)
    assert len(stored) == 1
    assert stored[0].tenant_id == tenant.id
    assert stored[0].channel.value == "email"
    assert stored[0].prompt_version == CHANNEL_POLICY_VERSION


def test_email_outranks_sms_when_both_are_available(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    reachable = make_contract(session_factory, tenant, consent_email=True, consent_sms=True)
    add_field(session_factory, reachable)
    add_field(session_factory, reachable, field_name=FieldName.PHONE, value=CONTACT_PHONE)
    headers = auth(client, tenant, make_user)

    body = decide(client, reachable.id, headers).json()

    # Fixed priority, not a score: the order is data a reader can predict.
    assert body["channel"] == "email"


def test_sms_is_chosen_when_email_consent_is_absent(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    reachable = make_contract(session_factory, tenant, consent_email=False, consent_sms=True)
    # An approved email is present but not consented to: the address must not be
    # used just because the pipeline happens to have it.
    add_field(session_factory, reachable)
    add_field(session_factory, reachable, field_name=FieldName.PHONE, value=CONTACT_PHONE)
    headers = auth(client, tenant, make_user)

    body = decide(client, reachable.id, headers).json()

    assert body["channel"] == "sms"
    assert body["confidence"] == pytest.approx(0.93)


def test_voice_is_chosen_when_only_voice_consent_exists(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    reachable = make_contract(session_factory, tenant, consent_email=False, consent_voice=True)
    add_field(session_factory, reachable, field_name=FieldName.PHONE, value=CONTACT_PHONE)
    headers = auth(client, tenant, make_user)

    assert decide(client, reachable.id, headers).json()["channel"] == "voice"


def test_an_auto_accepted_field_is_usable(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract, status=FieldStatus.AUTO_ACCEPTED)
    headers = auth(client, tenant, make_user)

    # Auto-accepted is the extractor's own confident path and needs no person.
    assert decide(client, contract.id, headers).status_code == 201


def test_an_escalated_contract_is_still_decided_on(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    escalated = make_contract(session_factory, tenant, status=ContractStatus.ESCALATED)
    add_field(session_factory, escalated)
    headers = auth(client, tenant, make_user)

    # The one status that must not be treated as closed: an escalated contract is
    # the contract most in need of a next attempt.
    assert decide(client, escalated.id, headers).status_code == 201


def test_an_unreadable_field_does_not_block_the_pipeline(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract)
    # Unreadable does not hold the pipeline (``blocks_pipeline``) and carries no
    # value, so it is simply not a candidate channel.
    add_field(
        session_factory,
        contract,
        field_name=FieldName.ADDRESS,
        value=None,
        status=FieldStatus.UNREADABLE,
    )
    headers = auth(client, tenant, make_user)

    assert decide(client, contract.id, headers).status_code == 201


# --- refusals -------------------------------------------------------------
def test_a_contract_with_no_consent_is_refused(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    unconsented = make_contract(session_factory, tenant, consent_email=False)
    add_field(session_factory, unconsented)
    headers = auth(client, tenant, make_user)

    response = decide(client, unconsented.id, headers)

    # The consent flags default to false, so this is the state of every contract
    # that has not been through ``PUT /consent``.
    assert response.status_code == 409
    assert refusal_code(response) == "NO_CONSENT"
    assert decisions(session_factory) == []


def test_an_opt_out_beats_a_full_grant(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    opted_out = make_contract(
        session_factory,
        tenant,
        consent_email=True,
        consent_sms=True,
        consent_voice=True,
        do_not_contact=True,
    )
    add_field(session_factory, opted_out)
    add_field(session_factory, opted_out, field_name=FieldName.PHONE, value=CONTACT_PHONE)
    headers = auth(client, tenant, make_user)

    response = decide(client, opted_out.id, headers)

    # A veto, not a channel choice: three grants do not outweigh one stop.
    assert response.status_code == 409
    assert refusal_code(response) == "CONTACT_REFUSED"
    assert decisions(session_factory) == []


def test_a_pending_field_blocks_the_decision(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract)
    add_field(
        session_factory,
        contract,
        field_name=FieldName.ADDRESS,
        value=None,
        status=FieldStatus.PENDING_REVIEW,
    )
    headers = auth(client, tenant, make_user)

    response = decide(client, contract.id, headers)

    assert response.status_code == 409
    assert refusal_code(response) == "PIPELINE_BLOCKED"
    # The field *name* is disclosed — the review queue already lists it — and the
    # count says how much work is outstanding. No value appears.
    details = response.json()["error"]["details"]
    assert details["blocking"] == 1
    assert details["fields"] == ["address"]


def test_a_rejected_field_blocks_the_decision(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract)
    add_field(
        session_factory,
        contract,
        field_name=FieldName.DOB,
        value=None,
        status=FieldStatus.REJECTED,
    )
    headers = auth(client, tenant, make_user)

    # A rejected field is the same hard stop as a pending one, because the two are
    # the same rule (``ExtractedField.blocks_pipeline``).
    assert refusal_code(decide(client, contract.id, headers)) == "PIPELINE_BLOCKED"


def test_the_block_is_reported_before_the_consent_gap(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    blocked = make_contract(session_factory, tenant, consent_email=False)
    add_field(
        session_factory,
        blocked,
        status=FieldStatus.PENDING_REVIEW,
    )
    headers = auth(client, tenant, make_user)

    # Two problems at once: the order of the gates is part of the contract with the
    # caller, because it decides which fix an operator is told to make first.
    assert refusal_code(decide(client, blocked.id, headers)) == "PIPELINE_BLOCKED"


def test_a_closed_contract_is_refused(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    closed = make_contract(session_factory, tenant, status=ContractStatus.RENEWED)
    add_field(session_factory, closed)
    headers = auth(client, tenant, make_user)

    response = decide(client, closed.id, headers)

    assert response.status_code == 409
    assert refusal_code(response) == "CONTRACT_CLOSED"


def test_expired_and_responded_contracts_are_closed_too(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    headers = auth(client, tenant, make_user)

    for index, status in enumerate((ContractStatus.EXPIRED, ContractStatus.RESPONDED)):
        closed = make_contract(session_factory, tenant, status=status)
        add_field(session_factory, closed, value=f"case{index}@example.test")
        assert refusal_code(decide(client, closed.id, headers)) == "CONTRACT_CLOSED"


def test_an_approved_non_contact_field_does_not_clear_the_value_gap(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    # The pipeline is clear (nothing pending) and consent is granted, so the only
    # remaining obstacle is that no approved value can satisfy the channel.
    add_field(session_factory, contract, field_name=FieldName.START_DATE, value="2025-01-01")
    headers = auth(client, tenant, make_user)

    response = decide(client, contract.id, headers)

    # "Nothing is blocking" is not the same as "we can reach them", and the two
    # refusals are different codes so an operator knows which is which.
    assert refusal_code(response) == "NO_CONTACT_VALUE"


def test_an_approved_field_with_no_value_is_not_a_contact_route(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    # Approved, but the extraction produced nothing: a row that cannot send.
    add_field(session_factory, contract, value=None, status=FieldStatus.APPROVED)
    headers = auth(client, tenant, make_user)

    response = decide(client, contract.id, headers)

    assert response.status_code == 409
    assert refusal_code(response) == "NO_CONTACT_VALUE"
    details = response.json()["error"]["details"]
    assert details["consented"] == ["email"]
    assert details["required_fields"] == ["email"]


def test_an_approved_address_is_not_a_contact_route(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    # ``Channel`` has no postal member, so an address is not reachability even when
    # a person has approved it.
    add_field(session_factory, contract, field_name=FieldName.ADDRESS, value="1 Test Street")
    headers = auth(client, tenant, make_user)

    assert refusal_code(decide(client, contract.id, headers)) == "NO_CONTACT_VALUE"


def test_a_contract_with_no_fields_at_all_is_refused(
    client: TestClient,
    contract: Contract,
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    headers = auth(client, tenant, make_user)

    # Nothing extracted is not the same as nothing to worry about.
    assert refusal_code(decide(client, contract.id, headers)) == "NO_CONTACT_VALUE"


def test_a_refusal_creates_no_decision_row(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    unconsented = make_contract(session_factory, tenant, consent_email=False)
    add_field(session_factory, unconsented)
    headers = auth(client, tenant, make_user)

    decide(client, unconsented.id, headers)

    # A refusal is the policy working, not a decision — storing one would record a
    # channel that was never permitted.
    assert decisions(session_factory) == []


# --- what must never be disclosed ----------------------------------------
def test_the_reasoning_contains_no_contact_value(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract)
    headers = auth(client, tenant, make_user)

    response = decide(client, contract.id, headers)

    # ``reasoning`` is plain text in a table any operator can read, and the value it
    # describes is encrypted precisely so that reading one is audited.
    assert CONTACT_EMAIL not in response.text
    stored = decisions(session_factory)[0]
    assert CONTACT_EMAIL not in stored.reasoning
    assert CONTACT_EMAIL not in json.dumps(stored.reasoning)


def test_the_reasoning_explains_the_choice_from_channel_names_only(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract)
    headers = auth(client, tenant, make_user)

    reasoning = decide(client, contract.id, headers).json()["reasoning"]

    assert "email" in reasoning
    assert "consent" in reasoning
    # Provenance is stated, so a reader can tell a deterministic row from a
    # model-backed one without consulting ``llm_model``.
    assert "no model call" in reasoning


def test_a_blocked_refusal_does_not_disclose_the_blocking_value(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    blocked = make_contract(session_factory, tenant)
    add_field(session_factory, blocked)
    add_field(
        session_factory,
        blocked,
        field_name=FieldName.PHONE,
        value=CONTACT_PHONE,
        status=FieldStatus.PENDING_REVIEW,
    )
    headers = auth(client, tenant, make_user)

    response = decide(client, blocked.id, headers)

    assert refusal_code(response) == "PIPELINE_BLOCKED"
    assert CONTACT_PHONE not in response.text


# --- history --------------------------------------------------------------
def test_a_rerun_appends_rather_than_replaces(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract)
    headers = auth(client, tenant, make_user)

    first = decide(client, contract.id, headers).json()
    second = decide(client, contract.id, headers).json()

    # Decisions are history, not state: "why did we email them in March?" stays
    # answerable after consent changes.
    assert first["id"] != second["id"]
    assert len(decisions(session_factory)) == 2


def test_history_is_newest_first(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract)
    headers = auth(client, tenant, make_user)
    first = decide(client, contract.id, headers).json()
    second = decide(client, contract.id, headers).json()

    body = client.get(DECISIONS_PATH, params={"contract_id": contract.id}, headers=headers).json()

    assert [item["id"] for item in body["items"]] == [second["id"], first["id"]]


def test_the_listing_is_empty_before_any_decision(
    client: TestClient,
    contract: Contract,
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    headers = auth(client, tenant, make_user)

    body = client.get(DECISIONS_PATH, params={"contract_id": contract.id}, headers=headers).json()

    assert body == {"items": [], "count": 0, "contract_id": contract.id}


def test_the_tenant_wide_listing_needs_no_contract_id(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract)
    headers = auth(client, tenant, make_user)
    decide(client, contract.id, headers)

    body = client.get(DECISIONS_PATH, headers=headers).json()

    assert body["count"] == 1
    assert body["contract_id"] is None


def test_the_listing_rejects_a_limit_outside_the_bound(
    client: TestClient,
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    headers = auth(client, tenant, make_user)

    # Bounded at the query, so no caller can ask for a whole tenant's history.
    assert client.get(DECISIONS_PATH, params={"limit": 0}, headers=headers).status_code == 422
    assert client.get(DECISIONS_PATH, params={"limit": 201}, headers=headers).status_code == 422


# --- roles and tenant isolation ------------------------------------------
def test_a_viewer_cannot_decide(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract)
    headers = auth(client, tenant, make_user, role=UserRole.VIEWER)

    # The decision sends nothing itself, but it is the row a dispatcher acts on.
    assert decide(client, contract.id, headers).status_code == 403
    assert decisions(session_factory) == []


def test_a_viewer_can_read_decisions(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract)
    operator_headers = auth(client, tenant, make_user)
    decide(client, contract.id, operator_headers)
    viewer_headers = auth(client, tenant, make_user, role=UserRole.VIEWER, email="viewer@acme.test")

    # No customer value appears in a decision, so reading one needs no reveal step.
    assert (
        client.get(
            DECISIONS_PATH, params={"contract_id": contract.id}, headers=viewer_headers
        ).status_code
        == 200
    )


def test_deciding_requires_authentication(client: TestClient, contract: Contract) -> None:
    assert client.post(DECISIONS_PATH, json={"contract_id": contract.id}).status_code == 401


def test_another_tenants_contract_is_not_found(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
    other_tenant: Tenant,
) -> None:
    foreign = make_contract(session_factory, other_tenant)
    add_field(session_factory, foreign)
    headers = auth(client, tenant, make_user)

    # 404 rather than 403: a 403 would confirm the id exists elsewhere.
    assert decide(client, foreign.id, headers).status_code == 404


def test_another_tenants_decisions_are_invisible(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
    other_tenant: Tenant,
) -> None:
    foreign = make_contract(session_factory, other_tenant)
    add_field(session_factory, foreign)
    foreign_headers = auth(client, other_tenant, make_user, email="ops@globex.test")
    decide(client, foreign.id, foreign_headers)
    headers = auth(client, tenant, make_user)

    body = client.get(DECISIONS_PATH, params={"contract_id": foreign.id}, headers=headers).json()

    # Empty rather than 404: the tenant predicate runs before the id, so the route
    # cannot be used to probe which contract ids exist.
    assert body["count"] == 0


def test_the_tenant_wide_listing_excludes_other_tenants(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
    other_tenant: Tenant,
) -> None:
    add_field(session_factory, contract)
    add_field(session_factory, make_contract(session_factory, other_tenant))
    headers = auth(client, tenant, make_user)
    decide(client, contract.id, headers)

    body = client.get(DECISIONS_PATH, headers=headers).json()

    assert body["count"] == 1


# --- recording consent ----------------------------------------------------
def consent_body(contract: Contract, **overrides: bool | int) -> dict[str, Any]:
    """The full replacement state the route requires, defaulted to "no consent"."""
    body: dict[str, Any] = {
        "consent_email": False,
        "consent_sms": False,
        "consent_voice": False,
        "do_not_contact": False,
        "version": contract.version,
    }
    body.update(overrides)
    return body


def test_consent_is_recorded_and_reported_back(
    client: TestClient,
    contract: Contract,
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    headers = auth(client, tenant, make_user)

    response = client.put(
        CONSENT_PATH.format(contract_id=contract.id),
        json=consent_body(contract, consent_email=True, consent_sms=True),
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["consent_email"] is True
    assert body["consent_sms"] is True
    assert body["consent_channels"] == ["email", "sms"]
    assert body["may_contact"] is True
    assert body["consent_updated_at"] is not None


def test_recording_consent_lets_a_decision_succeed(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    unconsented = make_contract(session_factory, tenant, consent_email=False)
    add_field(session_factory, unconsented)
    headers = auth(client, tenant, make_user)
    assert refusal_code(decide(client, unconsented.id, headers)) == "NO_CONSENT"

    client.put(
        CONSENT_PATH.format(contract_id=unconsented.id),
        json=consent_body(unconsented, consent_email=True),
        headers=headers,
    )

    # The whole point of the route: without it every contract is un-contactable.
    assert decide(client, unconsented.id, headers).status_code == 201


def test_recording_consent_is_audited(
    client: TestClient,
    unconsented: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    headers = auth(client, tenant, make_user)

    client.put(
        CONSENT_PATH.format(contract_id=unconsented.id),
        json=consent_body(unconsented, consent_email=True),
        headers=headers,
    )

    rows = audit_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].action == "contract.consent_updated"
    assert rows[0].entity == "contracts"
    assert rows[0].entity_id == str(unconsented.id)
    assert rows[0].actor_type == "user"
    # "Who said we could call this customer" has to be answerable later.
    assert json.loads(rows[0].after_state or "{}")["consent_email"] is True


def test_an_identical_resubmission_writes_nothing(
    client: TestClient,
    unconsented: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    headers = auth(client, tenant, make_user)
    path = CONSENT_PATH.format(contract_id=unconsented.id)
    body = consent_body(unconsented, consent_email=True)
    client.put(path, json=body, headers=headers)
    stored = load_contract(session_factory, unconsented.id)

    response = client.put(
        path,
        json={**body, "version": stored.version},
        headers=headers,
    )

    # An idempotent retry must not look like a second consent decision.
    assert response.status_code == 200
    assert len(audit_rows(session_factory)) == 1
    assert load_contract(session_factory, unconsented.id).version == stored.version


def test_a_stale_consent_version_is_a_conflict(
    client: TestClient,
    unconsented: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    headers = auth(client, tenant, make_user)
    path = CONSENT_PATH.format(contract_id=unconsented.id)
    client.put(path, json=consent_body(unconsented, consent_email=True), headers=headers)

    # Replay the original body: another operator has committed since it was loaded.
    response = client.put(path, json=consent_body(unconsented, consent_email=True), headers=headers)

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "VERSION_CONFLICT"
    # The winning row is handed back, so a lost update need not be diagnosed by
    # re-reading everything.
    assert error["details"]["current"]["version"] != unconsented.version


def test_a_stale_body_is_a_conflict_even_when_it_would_change_nothing(
    client: TestClient,
    unconsented: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    headers = auth(client, tenant, make_user)
    path = CONSENT_PATH.format(contract_id=unconsented.id)
    body = consent_body(unconsented, consent_email=True)
    client.put(path, json=body, headers=headers)

    # The replay *is* a no-op against current state, but the caller's snapshot is
    # still stale. Reporting 200 would hide that the row moved under them.
    stale = client.put(path, json=body, headers=headers)

    assert stale.status_code == 409
    # Only the accepted write is on record; the refused replay wrote nothing.
    assert len(audit_rows(session_factory)) == 1


def test_a_partial_consent_body_is_rejected(
    client: TestClient,
    contract: Contract,
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    headers = auth(client, tenant, make_user)

    response = client.put(
        CONSENT_PATH.format(contract_id=contract.id),
        json={"consent_email": True, "version": contract.version},
        headers=headers,
    )

    # Omission is a 422 rather than an implicit false: a caller must not silently
    # revoke a channel it never mentioned.
    assert response.status_code == 422


def test_an_opt_out_reports_no_contact_and_no_channels(
    client: TestClient,
    contract: Contract,
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    headers = auth(client, tenant, make_user)

    body = client.put(
        CONSENT_PATH.format(contract_id=contract.id),
        json=consent_body(contract, consent_email=True, do_not_contact=True),
        headers=headers,
    ).json()

    # The grant is still recorded (it is what they allowed); the veto is separate.
    assert body["consent_channels"] == ["email"]
    assert body["may_contact"] is False


def test_a_viewer_cannot_record_consent(
    client: TestClient,
    contract: Contract,
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    headers = auth(client, tenant, make_user, role=UserRole.VIEWER)

    response = client.put(
        CONSENT_PATH.format(contract_id=contract.id),
        json=consent_body(contract, consent_email=True),
        headers=headers,
    )

    assert response.status_code == 403


def test_consent_on_another_tenants_contract_is_not_found(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
    other_tenant: Tenant,
) -> None:
    foreign = make_contract(session_factory, other_tenant)
    headers = auth(client, tenant, make_user)

    response = client.put(
        CONSENT_PATH.format(contract_id=foreign.id),
        json=consent_body(foreign, consent_email=True),
        headers=headers,
    )

    assert response.status_code == 404
    assert audit_rows(session_factory) == []


# --- operator corrections (gap 19) ---------------------------------------
OVERRIDE_PATH = "/api/v1/agent/decisions/{contract_id}/override"
FLAG_PATH = "/api/v1/agent/decisions/{contract_id}/flag-review"


def reachable_contract(session_factory: sessionmaker[Session], tenant: Tenant) -> Contract:
    """A contract that may be emailed *or* texted, so an override has somewhere to go."""
    row = make_contract(session_factory, tenant, consent_email=True, consent_sms=True)
    add_field(session_factory, row)
    add_field(session_factory, row, field_name=FieldName.PHONE, value=CONTACT_PHONE)
    return row


def override(
    client: TestClient,
    contract_id: int,
    headers: dict[str, str],
    *,
    channel: str,
    version: int,
    reason: str | None = None,
) -> Any:
    body: dict[str, Any] = {"channel": channel, "version": version}
    if reason is not None:
        body["reason"] = reason
    return client.post(OVERRIDE_PATH.format(contract_id=contract_id), json=body, headers=headers)


def flag(
    client: TestClient,
    contract_id: int,
    headers: dict[str, str],
    *,
    version: int,
    reason: str | None = None,
) -> Any:
    body: dict[str, Any] = {"version": version}
    if reason is not None:
        body["reason"] = reason
    return client.post(FLAG_PATH.format(contract_id=contract_id), json=body, headers=headers)


def test_an_override_replaces_the_channel_and_preserves_the_policys_choice(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    contract = reachable_contract(session_factory, tenant)
    headers = auth(client, tenant, make_user)
    decided = decide(client, contract.id, headers).json()
    assert decided["channel"] == "email"

    response = override(
        client, contract.id, headers, channel="sms", version=decided["version"], reason="no email"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["channel"] == "sms"
    # The policy's own answer is kept, because "a person disagreed" is only
    # meaningful next to what they disagreed with.
    assert body["original_channel"] == "email"
    assert body["overridden_by"] is not None
    # The recommendation is edited, not re-created: same row, new generation.
    assert body["id"] == decided["id"]
    assert body["version"] == decided["version"] + 1


def test_an_override_appends_no_decision_row(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    contract = reachable_contract(session_factory, tenant)
    headers = auth(client, tenant, make_user)
    decided = decide(client, contract.id, headers).json()

    override(client, contract.id, headers, channel="sms", version=decided["version"])

    # A correction is a fact about *that* recommendation, so history keeps its shape.
    assert len(decisions(session_factory)) == 1


def test_an_override_is_audited_with_both_channels(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    contract = reachable_contract(session_factory, tenant)
    headers = auth(client, tenant, make_user)
    decided = decide(client, contract.id, headers).json()

    override(
        client, contract.id, headers, channel="sms", version=decided["version"], reason="bounced"
    )

    rows = audit_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].action == "agent_decision.overridden"
    assert rows[0].entity == "agent_decisions"
    assert rows[0].entity_id == str(decided["id"])
    assert rows[0].actor_type == "user"
    assert rows[0].reason == "bounced"
    # Only channel names and flags live in the snapshot — never a contact value.
    assert json.loads(rows[0].before_state or "{}")["channel"] == "email"
    assert json.loads(rows[0].after_state or "{}")["channel"] == "sms"
    assert CONTACT_EMAIL not in (rows[0].after_state or "")
    assert CONTACT_PHONE not in (rows[0].after_state or "")


def test_a_second_override_still_reports_the_policys_original_channel(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    contract = reachable_contract(session_factory, tenant)
    headers = auth(client, tenant, make_user)
    decided = decide(client, contract.id, headers).json()
    first = override(client, contract.id, headers, channel="sms", version=decided["version"]).json()

    second = override(
        client, contract.id, headers, channel="email", version=first["version"]
    ).json()

    # ``original_channel`` is written once: the second operator's predecessor is not
    # the policy, so overwriting it would lose the only record of what it decided.
    assert second["channel"] == "email"
    assert second["original_channel"] == "email"


def test_an_override_to_an_unpermitted_channel_is_refused(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    # Email-only consent, but a perfectly approved phone number is on file: the
    # value exists and still must not be usable, because nobody consented to SMS.
    add_field(session_factory, contract)
    add_field(session_factory, contract, field_name=FieldName.PHONE, value=CONTACT_PHONE)
    headers = auth(client, tenant, make_user)
    decided = decide(client, contract.id, headers).json()

    response = override(client, contract.id, headers, channel="sms", version=decided["version"])

    assert response.status_code == 409
    error = response.json()["error"]
    # Its own code, distinct from "no channel at all": the fix is to pick a
    # permitted channel, not to go review a field.
    assert error["code"] == "CHANNEL_NOT_PERMITTED"
    assert error["details"]["permitted"] == ["email"]
    assert decisions(session_factory)[0].channel.value == "email"
    assert audit_rows(session_factory) == []


def test_an_override_re_runs_the_contact_gates(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract)
    headers = auth(client, tenant, make_user)
    decided = decide(client, contract.id, headers).json()
    # The customer withdraws everything after the recommendation was made. A stored
    # decision is a snapshot; the override must be judged against today's state.
    client.put(
        CONSENT_PATH.format(contract_id=contract.id),
        json=consent_body(contract, do_not_contact=True),
        headers=headers,
    )

    response = override(client, contract.id, headers, channel="email", version=decided["version"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONTACT_REFUSED"


def test_a_stale_override_version_is_a_conflict(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    contract = reachable_contract(session_factory, tenant)
    headers = auth(client, tenant, make_user)
    decided = decide(client, contract.id, headers).json()
    override(client, contract.id, headers, channel="sms", version=decided["version"])

    # Replay the original version: another operator's correction is already in.
    response = override(client, contract.id, headers, channel="sms", version=decided["version"])

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "VERSION_CONFLICT"
    # The winning row comes back, so the loser can re-render instead of guessing.
    assert error["details"]["current"]["channel"] == "sms"
    # Only the accepted correction is on record.
    assert len(audit_rows(session_factory)) == 1


def test_an_override_before_any_decision_is_a_conflict(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract)
    headers = auth(client, tenant, make_user)

    response = override(client, contract.id, headers, channel="email", version=1)

    # 409, not 404: the contract exists, there is just nothing to correct yet.
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NO_DECISION"


def test_flag_review_marks_the_current_recommendation(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract)
    headers = auth(client, tenant, make_user)
    decided = decide(client, contract.id, headers).json()
    assert decided["needs_review"] is False

    response = flag(client, contract.id, headers, version=decided["version"], reason="looks wrong")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["needs_review"] is True
    # Flagging takes the recommendation out of the send path without touching the
    # channel, so nothing about the choice itself is lost.
    assert body["channel"] == decided["channel"]
    assert body["version"] == decided["version"] + 1


def test_flag_review_is_audited(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract)
    headers = auth(client, tenant, make_user)
    decided = decide(client, contract.id, headers).json()

    flag(client, contract.id, headers, version=decided["version"], reason="call them instead")

    rows = audit_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].action == "agent_decision.flagged"
    assert rows[0].entity == "agent_decisions"
    assert rows[0].reason == "call them instead"
    assert json.loads(rows[0].before_state or "{}")["needs_review"] is False
    assert json.loads(rows[0].after_state or "{}")["needs_review"] is True


def test_flag_review_does_not_re_run_the_contact_gates(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract)
    headers = auth(client, tenant, make_user)
    decided = decide(client, contract.id, headers).json()
    client.put(
        CONSENT_PATH.format(contract_id=contract.id),
        json=consent_body(contract, do_not_contact=True),
        headers=headers,
    )

    response = flag(client, contract.id, headers, version=decided["version"])

    # Every other refusal here answers "may we contact them?". Flagging is the
    # opposite request, so requiring consent would withhold the safe action exactly
    # when the contract looks worst.
    assert response.status_code == 200, response.text


def test_a_flagged_decision_is_visible_in_the_listing(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract)
    headers = auth(client, tenant, make_user)
    decided = decide(client, contract.id, headers).json()
    flag(client, contract.id, headers, version=decided["version"])

    body = client.get(DECISIONS_PATH, params={"contract_id": contract.id}, headers=headers).json()

    # ``dispatch`` refuses on this flag, so it has to be readable by the people who
    # need to clear it.
    assert body["items"][0]["needs_review"] is True


def test_flagging_an_already_flagged_decision_changes_nothing(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract)
    headers = auth(client, tenant, make_user)
    decided = decide(client, contract.id, headers).json()
    first = flag(client, contract.id, headers, version=decided["version"]).json()

    second = flag(client, contract.id, headers, version=first["version"])

    # A retry is a retry, not a second flag: no UPDATE is emitted, so the version
    # does not advance and the state stays where the first call put it.
    assert second.status_code == 200
    assert second.json()["version"] == first["version"]


def test_a_viewer_cannot_override_or_flag(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract)
    operator_headers = auth(client, tenant, make_user)
    decided = decide(client, contract.id, operator_headers).json()
    viewer_headers = auth(client, tenant, make_user, role=UserRole.VIEWER, email="v@acme.test")

    assert (
        override(
            client, contract.id, viewer_headers, channel="email", version=decided["version"]
        ).status_code
        == 403
    )
    assert flag(client, contract.id, viewer_headers, version=decided["version"]).status_code == 403
    assert audit_rows(session_factory) == []


def test_correcting_another_tenants_contract_is_not_found(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
    other_tenant: Tenant,
) -> None:
    foreign = reachable_contract(session_factory, other_tenant)
    foreign_headers = auth(client, other_tenant, make_user, email="ops@globex.test")
    decide(client, foreign.id, foreign_headers)
    headers = auth(client, tenant, make_user)

    # 404 rather than 409: the id must not be confirmed to exist elsewhere.
    assert override(client, foreign.id, headers, channel="sms", version=1).status_code == 404
    assert flag(client, foreign.id, headers, version=1).status_code == 404


def test_a_rerun_count_records_how_many_recommendations_came_before(
    client: TestClient,
    contract: Contract,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    add_field(session_factory, contract)
    headers = auth(client, tenant, make_user)

    first = decide(client, contract.id, headers).json()
    second = decide(client, contract.id, headers).json()

    # Stamped at append time, so "re-rolled four times" is readable from the row
    # itself rather than only from the history listing.
    assert first["rerun_count"] == 0
    assert second["rerun_count"] == 1


def test_the_override_body_cannot_carry_a_contact_value(
    client: TestClient,
    session_factory: sessionmaker[Session],
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    contract = reachable_contract(session_factory, tenant)
    headers = auth(client, tenant, make_user)
    decided = decide(client, contract.id, headers).json()

    response = client.post(
        OVERRIDE_PATH.format(contract_id=contract.id),
        json={
            "channel": "sms",
            "version": decided["version"],
            "recipient": CONTACT_PHONE,
        },
        headers=headers,
    )

    # Extra keys are ignored rather than persisted: the recipient is the approved
    # field's value, and an override may only choose *among* permitted channels.
    assert response.status_code == 200
    assert CONTACT_PHONE not in response.text
