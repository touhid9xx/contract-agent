"""The call-outcome callback: what the dialler learned that ARI alone cannot say (P2-F).

``D10-144`` drew this line explicitly: ``AriVoiceSender`` records a call *placed*, and
whether a person or an answering machine picked up happens inside the Stasis application.
This route is the smaller of the two ways to close that gap — Asterisk's own ``AMD()``
decides in the dialplan, and the verdict is posted back here.

The properties pinned below are the ones that make the second unauthenticated,
state-changing endpoint in this API safe:

* **The secret is the switch, and it is its own secret.** With none configured the route
  answers ``404``; with one configured an unsigned or wrongly signed request gets ``401``
  and moves nothing. The SMS gateway's secret is deliberately *not* reused.
* **The answer never differs.** Matched, unmatched, already recorded, ``NOTSURE`` — all
  ``202 {"status": "accepted"}``. The operator-facing truth is in
  ``voice_outcome_total{outcome}``, which is not reachable from outside.
* **AMD declining to decide writes nothing.** ``NOTSURE``/``DONTCARE`` is not one of the
  two verdicts; folding it into ``machine`` would record that we reached voicemail when
  we do not know that. A verdict outside AMD's vocabulary is a ``400``, because a typo
  is a fact about the dialplan rather than about the call.
* **Only a row we actually sent can be moved.** A callback naming a channel id no row
  holds, or one whose row never left ``pending``/``failed``, writes nothing: a callback
  is not evidence that a failed send succeeded.
* **A retry is not a second state change.** ``Notification`` carries an optimistic-lock
  ``version``, so "wrote nothing" is observable rather than merely asserted.

Not asserted here, because it is not true: that this has ever met a real Asterisk. Every
request below is built and signed in-process, and the AMD verdict itself is not measured
— see ``D10-154``'s limits.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.v1.inbound import MAX_WEBHOOK_BYTES, SIGNATURE_HEADER
from app.api.v1.inbound_voice import AMD_VERDICTS, STATUS_FOR_VERDICT
from app.core import metrics
from app.core.config import Settings
from app.db.base import utc_now
from app.db.enums import Channel, ContractStatus, NotificationStatus
from app.db.models import Contract, Notification, Tenant
from app.db.session import session_scope
from app.main import create_app
from app.workers.dispatch import notification_idempotency_key

OUTCOME_PATH = "/api/v1/voice/outcome"

#: The secret Asterisk signs callbacks with. Protects in-memory state only.
SECRET = "voice-outcome-test-webhook-secret-0123"

#: The ARI channel id our own dial returned, echoed back by the dialplan.
CHANNEL_ID = "1755012345.42"

#: Every label this route can move. Named once so the metric test cannot read half.
OUTCOMES = ("human", "machine", "unknown", "unmatched")


def utc_today() -> date:
    """Today on the UTC calendar, the way ``scheduled_date`` is filled in."""
    return datetime.now(UTC).date()


# --- fixtures -------------------------------------------------------------
@pytest.fixture
def voice_settings(settings: Settings) -> Settings:
    """Settings with a call-outcome secret configured.

    ``model_copy`` rather than a fresh ``Settings``, for the reason
    ``test_inbound.py`` gives: the copy keeps the in-memory database and the lowered
    Argon2 cost conftest already agreed on.

    It also bypasses this module's start-up validator, which refuses a
    ``VOICE_OUTCOME_SECRET`` without ``VOICE_TRANSPORT=ari``. That check exists to
    guarantee a notification row *can* carry the channel id a callback names — and the
    fixtures below write exactly such a row directly, which is the fact the validator
    would otherwise be standing in for. The route itself reads only the secret.
    """
    return settings.model_copy(update={"voice_outcome_secret": SECRET})


@pytest.fixture
def voice_client(
    voice_settings: Settings, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    """A client over the whole app, so routing and dependencies are real."""
    app = create_app(voice_settings, session_factory=session_factory)
    with TestClient(app) as test_client:
        yield test_client


# --- helpers --------------------------------------------------------------
def make_contract(session_factory: sessionmaker[Session], owner: Tenant) -> Contract:
    with session_scope(session_factory) as session:
        row = Contract(
            tenant_id=owner.id,
            customer_ref="A. Rahman",
            timezone="UTC",
            status=ContractStatus.ACTIVE,
        )
        session.add(row)
        session.flush()
        return row


def make_sent_notification(
    session_factory: sessionmaker[Session],
    contract: Contract,
    *,
    provider_message_id: str = CHANNEL_ID,
    status: NotificationStatus = NotificationStatus.SENT,
    slot_offset: int = 0,
) -> int:
    """Insert one voice row as ``dispatch`` leaves it after a successful dial.

    Written directly rather than dispatched, because the shipped voice transport is
    ``none``: a row that reached ``sent`` is unreachable through the real send path
    until an ARI deployment exists, and standing up a fake dialler here would only test
    the fake. What this route reads is the row, not how it was produced.

    ``provider_message_id`` is set in the same commit as ``status`` by
    ``app.workers.dispatch``, which is what makes a callback able to name a row at all.
    ``slot_offset`` varies the reminder slot so a test can stack several rows on one
    contract without colliding on ``uq_notification_slot``.
    """
    slot = utc_today() - timedelta(days=20 + slot_offset)
    with session_scope(session_factory) as session:
        row = Notification(
            tenant_id=contract.tenant_id,
            contract_id=contract.id,
            channel=Channel.VOICE,
            template="T-30",
            status=status,
            scheduled_date=slot,
            scheduled_at=None,
            sent_at=utc_now() if status is NotificationStatus.SENT else None,
            idempotency_key=notification_idempotency_key(contract.id, "T-30", slot),
            attempts=1,
            provider_message_id=provider_message_id,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def body(channel_id: str = CHANNEL_ID, verdict: str = "HUMAN") -> bytes:
    """A dialler payload, as the exact bytes the signature covers."""
    return json.dumps({"channel_id": channel_id, "verdict": verdict}).encode("utf-8")


def sign(payload: bytes, secret: str = SECRET) -> str:
    """The hex HMAC-SHA256 the dialplan puts in ``X-Signature``."""
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def post(client: TestClient, payload: bytes, *, signature: str | None = None) -> Response:
    """Deliver a payload, signing it unless a caller passes a signature to forge."""
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers[SIGNATURE_HEADER] = signature
    return client.post(OUTCOME_PATH, content=payload, headers=headers)


def load_notification(session_factory: sessionmaker[Session], notification_id: int) -> Notification:
    with session_scope(session_factory) as session:
        row = session.get(Notification, notification_id)
        assert row is not None
        return row


def status_of(session_factory: sessionmaker[Session], notification_id: int) -> NotificationStatus:
    """The stored status of one row, so each assertion stays one line and one claim."""
    return load_notification(session_factory, notification_id).status


def statuses(session_factory: sessionmaker[Session]) -> list[NotificationStatus]:
    """Every notification status in the database, whichever tenant wrote it."""
    with session_scope(session_factory) as session:
        return list(session.execute(select(Notification.status)).scalars())


# --- the two verdicts that exist -------------------------------------------
def test_a_human_verdict_marks_the_call_answered(
    voice_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """The behaviour the whole route exists for: a person picked up, and we know it."""
    contract = make_contract(session_factory, tenant)
    notification_id = make_sent_notification(session_factory, contract)
    payload = body(verdict="HUMAN")

    response = post(voice_client, payload, signature=sign(payload))

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    stored = load_notification(session_factory, notification_id)
    assert stored.status is NotificationStatus.ANSWERED
    # One column moved and no second fact was invented: the row still names the same
    # call, and no audio, transcript or duration was recorded anywhere.
    assert stored.provider_message_id == CHANNEL_ID


def test_a_machine_verdict_marks_the_call_a_machine(
    voice_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """A voicemail reached a mailbox, not a person — and the row says which.

    ``machine`` counts as contact in ``CONTACTED_STATUSES`` and is deliberately *not* a
    response, which is exactly the silence an escalation exists to catch. Writing it as
    ``answered`` would hide a mailbox full of unheard reminders.
    """
    contract = make_contract(session_factory, tenant)
    notification_id = make_sent_notification(session_factory, contract)
    payload = body(verdict="MACHINE")

    response = post(voice_client, payload, signature=sign(payload))

    assert response.status_code == 202
    assert status_of(session_factory, notification_id) is NotificationStatus.MACHINE


def test_the_verdict_is_read_in_the_dialplans_own_spelling(
    voice_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """``AMD_STATUS=$AMDSTATUS`` is the expected dialplan, so ``HUMAN`` is the wire form.

    Lower case and surrounding whitespace are still accepted, because refusing them
    would lose real verdicts to a naming preference rather than to anything about the
    call. Every token AMD can produce is mapped, and ``DONTCARE`` is "no opinion" rather
    than "machine".
    """
    contract = make_contract(session_factory, tenant)
    notification_id = make_sent_notification(session_factory, contract)
    payload = body(verdict="  human ")

    response = post(voice_client, payload, signature=sign(payload))

    assert response.status_code == 202
    assert status_of(session_factory, notification_id) is NotificationStatus.ANSWERED
    assert set(AMD_VERDICTS) == {"HUMAN", "MACHINE", "NOTSURE", "DONTCARE"}
    # Every verdict names an outcome, and no more than that: ``unmatched`` is not
    # among them because a verdict is read only *after* a row has matched, so no
    # spelling the dialplan can send is ever counted as a lookup that found nothing.
    # Stated as a difference rather than as a fourth label so that adding an AMD token
    # without an outcome, or an outcome no token can reach, both fail here.
    assert set(AMD_VERDICTS.values()) == set(OUTCOMES) - {"unmatched"}
    # ``unknown`` has no status, which is the code refusing to invent one.
    assert set(STATUS_FOR_VERDICT.values()) == {
        NotificationStatus.ANSWERED,
        NotificationStatus.MACHINE,
    }


# --- AMD declining to decide ----------------------------------------------
def test_amd_declining_to_decide_writes_no_status(
    voice_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """``NOTSURE`` is counted and written nowhere.

    AMD decides on audio energy, not on speech, so a refusal is a real outcome and the
    honest record of it is *no change*: the row still says "sent, outcome unknown".
    Folding it into ``machine`` would record something that did not happen.
    """
    contract = make_contract(session_factory, tenant)
    notification_id = make_sent_notification(session_factory, contract)
    before = load_notification(session_factory, notification_id).version
    payload = body(verdict="NOTSURE")

    response = post(voice_client, payload, signature=sign(payload))

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    stored = load_notification(session_factory, notification_id)
    assert stored.status is NotificationStatus.SENT
    assert stored.version == before


def test_a_verdict_outside_the_diallers_vocabulary_is_malformed(
    voice_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """A typo is a fact about the dialplan, so it is a ``400`` rather than ``unknown``.

    Folding an unrecognised word into ``unknown`` would make a misconfigured dialplan
    indistinguishable from calls AMD could not decide — and only the first is fixable.
    """
    contract = make_contract(session_factory, tenant)
    notification_id = make_sent_notification(session_factory, contract)
    payload = body(verdict="MAYBE")

    response = post(voice_client, payload, signature=sign(payload))

    assert response.status_code == 400
    assert status_of(session_factory, notification_id) is NotificationStatus.SENT


def test_a_body_that_is_not_a_call_outcome_is_malformed(
    voice_client: TestClient,
) -> None:
    """Signed but the wrong shape: a ``400`` that says nothing about why."""
    payload = json.dumps({"verdict": "HUMAN"}).encode("utf-8")

    response = post(voice_client, payload, signature=sign(payload))

    assert response.status_code == 400


def test_an_empty_channel_id_is_malformed_rather_than_matching_the_unsent_rows(
    voice_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """``""`` must never be the handle.

    Every unsent row shares ``NULL``, and ``NULL`` is not ``""`` — but an empty
    comparison is one refactor away from being one, which is why the payload model
    refuses it before the lookup runs.
    """
    contract = make_contract(session_factory, tenant)
    notification_id = make_sent_notification(session_factory, contract)
    payload = json.dumps({"channel_id": "", "verdict": "HUMAN"}).encode("utf-8")

    response = post(voice_client, payload, signature=sign(payload))

    assert response.status_code == 400
    assert status_of(session_factory, notification_id) is NotificationStatus.SENT


def test_an_oversized_body_is_refused_before_the_signature_is_checked(
    voice_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """``413`` and not ``401``, which pins the order: the cheap gate runs first.

    Bounding the body before spending an HMAC over it is what stops an unauthenticated
    caller buying CPU with uploads, so the unsigned oversize case has to reach the size
    gate rather than the comparison.
    """
    contract = make_contract(session_factory, tenant)
    notification_id = make_sent_notification(session_factory, contract)
    payload = b"x" * (MAX_WEBHOOK_BYTES + 1)

    response = post(voice_client, payload)

    assert response.status_code == 413
    assert status_of(session_factory, notification_id) is NotificationStatus.SENT


# --- the refusals ---------------------------------------------------------
def test_a_forged_signature_is_refused_and_moves_nothing(
    voice_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """Signed with the wrong secret: the callback never becomes an outcome."""
    contract = make_contract(session_factory, tenant)
    notification_id = make_sent_notification(session_factory, contract)
    payload = body()

    response = post(voice_client, payload, signature=sign(payload, secret="wrong-secret"))

    assert response.status_code == 401
    assert status_of(session_factory, notification_id) is NotificationStatus.SENT


def test_a_delivery_with_no_signature_is_refused(
    voice_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """An unsigned request is not "trusted by default" — it is a ``401``."""
    contract = make_contract(session_factory, tenant)
    notification_id = make_sent_notification(session_factory, contract)

    response = voice_client.post(OUTCOME_PATH, content=body())

    assert response.status_code == 401
    assert status_of(session_factory, notification_id) is NotificationStatus.SENT


def test_a_body_altered_after_signing_is_refused(
    voice_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """The signature covers the bytes, so a swapped verdict invalidates it.

    This is the case a "hash the parsed fields" design would miss, and the reason both
    signed webhooks read the raw body before parsing anything.
    """
    contract = make_contract(session_factory, tenant)
    notification_id = make_sent_notification(session_factory, contract)
    original = body(verdict="MACHINE")
    tampered = body(verdict="HUMAN")

    response = post(voice_client, tampered, signature=sign(original))

    assert response.status_code == 401
    assert status_of(session_factory, notification_id) is NotificationStatus.SENT


def test_the_sms_gateway_secret_does_not_authenticate_a_call_outcome(
    voice_settings: Settings,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """Two webhooks, two secrets: one leaked gateway secret must not invent outcomes.

    Signed with the *SMS* secret against a deployment that has both configured — the
    deployment where a shared-comparison refactor could quietly make them
    interchangeable.
    """
    contract = make_contract(session_factory, tenant)
    notification_id = make_sent_notification(session_factory, contract)
    both = voice_settings.model_copy(
        update={"inbound_webhook_secret": "sms-gateway-secret-0123456789abcdef"}
    )
    app = create_app(both, session_factory=session_factory)
    payload = body()

    with TestClient(app) as test_client:
        response = post(
            test_client,
            payload,
            signature=sign(payload, secret=both.inbound_webhook_secret),
        )

    assert response.status_code == 401
    assert status_of(session_factory, notification_id) is NotificationStatus.SENT


def test_without_a_secret_the_endpoint_does_not_exist(
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    """``404``, not ``403``: a scanner learns nothing about a switched-off route.

    ``voice_outcome_secret`` is the switch — there is no separate on/off variable,
    because the callback has exactly one possible author and "can we be told?" and "how
    do we authenticate the teller?" are the same question.
    """
    app = create_app(
        settings.model_copy(update={"voice_outcome_secret": ""}),
        session_factory=session_factory,
    )
    payload = body()

    with TestClient(app) as test_client:
        response = post(test_client, payload, signature=sign(payload))

    assert response.status_code == 404


# --- no oracle, and the unmatched cases -----------------------------------
def test_an_unmatched_channel_id_gets_the_same_answer_as_a_matched_one(
    voice_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """Identical status *and* body, and the unmatched callback writes nothing.

    The caller is our own dialler, but the reply is still a shape a log reader could
    mine — and a dialplan whose channel id has drifted from ours must show up in
    ``voice_outcome_total{outcome="unmatched"}`` rather than in the response.
    """
    contract = make_contract(session_factory, tenant)
    notification_id = make_sent_notification(session_factory, contract)

    matched = body(channel_id=CHANNEL_ID, verdict="HUMAN")
    unmatched = body(channel_id="1755099999.99", verdict="HUMAN")

    first = post(voice_client, matched, signature=sign(matched))
    second = post(voice_client, unmatched, signature=sign(unmatched))

    assert first.status_code == second.status_code == 202
    assert first.json() == second.json() == {"status": "accepted"}
    assert status_of(session_factory, notification_id) is NotificationStatus.ANSWERED


def test_a_row_that_was_never_sent_is_not_resurrected(
    voice_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """A callback is not evidence that a failed send succeeded.

    Treating it as an outcome would let a stray callback turn ``failed`` into
    ``answered`` — a claim about a call nobody made.
    """
    contract = make_contract(session_factory, tenant)
    notification_id = make_sent_notification(
        session_factory, contract, status=NotificationStatus.FAILED
    )
    payload = body(verdict="HUMAN")

    response = post(voice_client, payload, signature=sign(payload))

    assert response.status_code == 202
    assert status_of(session_factory, notification_id) is NotificationStatus.FAILED


def test_a_callback_moves_only_the_row_its_channel_id_names(
    voice_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    other_tenant: Tenant,
) -> None:
    """The lookup crosses tenants by design, and the *write* does not widen with it.

    No tenant is named in the payload — accepting one would let a caller redirect an
    outcome onto somebody else's row — so the provider-issued channel id is the only
    handle, and it is one this deployment's own dial returned.
    """
    ours = make_contract(session_factory, tenant)
    theirs = make_contract(session_factory, other_tenant)
    our_row = make_sent_notification(session_factory, ours, provider_message_id="chan-ours")
    their_row = make_sent_notification(session_factory, theirs, provider_message_id="chan-theirs")
    payload = body(channel_id="chan-theirs", verdict="MACHINE")

    response = post(voice_client, payload, signature=sign(payload))

    assert response.status_code == 202
    assert status_of(session_factory, their_row) is NotificationStatus.MACHINE
    assert status_of(session_factory, our_row) is NotificationStatus.SENT


def test_the_second_identical_callback_writes_nothing_new(
    voice_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """A dialler that retries a slow ``202`` is not a second state change.

    Read through ``Notification.version``, the optimistic-lock column: the first
    callback bumps it, the replay leaves it alone. That is what makes "wrote nothing"
    observable instead of asserted.
    """
    contract = make_contract(session_factory, tenant)
    notification_id = make_sent_notification(session_factory, contract)
    initial = load_notification(session_factory, notification_id).version
    payload = body()

    first = post(voice_client, payload, signature=sign(payload))
    after_first = load_notification(session_factory, notification_id).version
    again = post(voice_client, payload, signature=sign(payload))

    assert first.status_code == again.status_code == 202
    assert after_first > initial
    assert load_notification(session_factory, notification_id).version == after_first
    assert statuses(session_factory) == [NotificationStatus.ANSWERED]


# --- the metric -----------------------------------------------------------
def sample(text: str, outcome: str) -> float:
    """Read one labelled sample out of a Prometheus scrape, ``0`` when absent.

    A labelled counter publishes nothing until it is first incremented, so "absent"
    and "zero" are the same thing here and both mean "this path was not taken".
    """
    match = re.search(rf'voice_outcome_total\{{outcome="{outcome}"\}} ([0-9.]+)', text)
    return float(match.group(1)) if match else 0.0


def test_every_outcome_the_route_can_reach_is_counted(
    voice_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """The operator-facing truth lives in the series, so each branch has to move it.

    Read as a delta: the registry is process-wide, and other tests here have already
    moved these counters. ``unmatched`` is the number that matters — it means a call was
    placed that this database has no record of, or the callback arrived before the
    dispatch committed — and a standing ``unknown`` is a deployment whose every call AMD
    refuses to judge.
    """
    before = {name: sample(metrics.render(), name) for name in OUTCOMES}
    contract = make_contract(session_factory, tenant)
    make_sent_notification(session_factory, contract, provider_message_id="chan-a")
    make_sent_notification(session_factory, contract, provider_message_id="chan-b", slot_offset=1)
    make_sent_notification(session_factory, contract, provider_message_id="chan-c", slot_offset=2)

    for payload in (
        body(channel_id="chan-a", verdict="HUMAN"),
        body(channel_id="chan-b", verdict="MACHINE"),
        body(channel_id="chan-c", verdict="NOTSURE"),
        body(channel_id="chan-missing", verdict="HUMAN"),
    ):
        assert post(voice_client, payload, signature=sign(payload)).status_code == 202

    after = {name: sample(metrics.render(), name) for name in OUTCOMES}
    assert after == {name: before[name] + 1 for name in OUTCOMES}
