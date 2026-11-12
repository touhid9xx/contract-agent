"""Inbound SMS: the one surface a customer can reach without an account (P2-E).

Everything else in this API assumes a principal. An operator has a token, a worker has
a clock, a CLI has a shell — and a person who received a reminder text and wants it to
stop has none of those. Before this route, ``do_not_contact`` could only be set by an
operator or by ``consent-import``, which means the only way to stop the messages was to
reach a human at the company and convince them. This is the route that makes the
customer's own word sufficient.

Four decisions shape it, and each one is the fail-safe direction:

1. **Signed, over the raw bytes.** The gateway authenticates with
   ``X-Signature: hex(HMAC-SHA256(secret, body))``. The digest is computed over the
   bytes as received, before parsing, because a signature over a *re-serialized* body
   is a signature over something the sender never signed. ``compare_digest`` so the
   comparison does not leak the expected value byte by byte.

2. **The reply is always the same.** Every accepted outcome — opted out, already opted
   out, unknown number, not a stop word — returns ``202 {"status": "accepted"}``. The
   response goes to the gateway rather than to the customer, so the oracle here is a
   third party learning which numbers are ours. The operator-facing truth goes to
   ``sms_inbound_total{outcome}`` and the log, which are not reachable from outside.

3. **A tenant is never named.** The webhook carries a signature, a number, and a word.
   The tenants come from whichever contract rows hold that number
   (:func:`~app.db.repositories.fields.contracts_for_contact`), and each row's own
   ``tenant_id`` scopes its audit entry. This is the only cross-tenant read reachable
   from an unauthenticated request, and it has to be: asking a stranger to name their
   tenant before you stop texting them is not an opt-out.

4. **Suppression fails safe; matching does not have to.** A low-confidence or
   unapproved phone extraction still matches here. Honouring a STOP that might be a
   wrong number only *withholds* a message; ignoring one sends to somebody who asked
   us to stop. Between those two errors the false suppression wins every time.

What this route deliberately does **not** do: store the message, reply to it, or record
anything about a number that did not match. The body is read for one word and dropped,
and an unmatched sender leaves no row anywhere — a table of "phone numbers strangers
texted us from" would be exactly the kind of secondary data an opt-out must not create.
"""

from __future__ import annotations

import hmac
from hashlib import sha256
from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from app.api.deps import app_settings, blind_index_or_503, get_request_session
from app.core.blind_index import ContactBlindIndex, contact_digest
from app.core.config import Settings
from app.core.metrics import SMS_INBOUND_TOTAL
from app.core.middleware import correlation_id_or_new
from app.db.enums import FieldName
from app.db.models import Contract
from app.db.repositories.audit import (
    ACTION_CONSENT_OPTED_OUT,
    ACTOR_CUSTOMER,
    AUDIT_ENTITY_CONTRACT,
    AuditLogRepository,
)
from app.db.repositories.contracts import apply_consent, consent_flags
from app.db.repositories.fields import contracts_for_contact
from app.workers.extraction import normalize_value

__all__ = ["router"]

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/inbound", tags=["inbound"])

#: Header the gateway signs with. Named here so the route and its tests cannot drift.
SIGNATURE_HEADER: Final = "X-Signature"

#: A webhook is a few hundred bytes. Bounded so a *malformed* caller cannot make this
#: process hold a large body in memory before the signature has been checked; the
#: check itself has to happen after the body exists, so the size gate is the only
#: control available ahead of it. 8 KiB is generous for one text message.
MAX_WEBHOOK_BYTES: Final = 8 * 1024

#: Words that withdraw consent. Deliberately a closed set rather than "contains stop":
#: ``STOP`` delivered as a keyword is matched by the gateway, which is the normal path;
#: this set exists so a message that reaches us as a bare keyword is honoured even if
#: the gateway's own stop handling is misconfigured. It is a second line, not the only
#: one, and it is an exact match so "stop by the office" is a person talking, not an
#: opt-out. ``STOPALL`` and the carrier synonyms are included because different
#: networks use different ones and a customer typing the word they were told to type
#: must never be the reason a message keeps arriving.
STOP_WORDS: Final = frozenset(
    {
        "stop",
        "stopall",
        "unsubscribe",
        "cancel",
        "end",
        "quit",
        "opt out",
        "optout",
    }
)

#: Outcome labels. Fixed vocabulary so the metric cannot become a high-cardinality
#: description of arbitrary input.
OUTCOME_OPTED_OUT: Final = "opted_out"
OUTCOME_ALREADY_OPTED_OUT: Final = "already_opted_out"
OUTCOME_UNMATCHED: Final = "unmatched"
OUTCOME_IGNORED: Final = "ignored"
OUTCOME_REJECTED: Final = "rejected"
OUTCOME_MALFORMED: Final = "malformed"


class WebhookMessage(BaseModel):
    """The slice of the gateway's payload this route reads.

    ``extra="ignore"`` rather than ``forbid``: a gateway adds fields on its own
    schedule, and refusing a webhook because it grew a field would turn their release
    into our outage. Aliases match the wire names, so the code reads in ``snake_case``
    while the contract stays the gateway's.

    There is no ``tenant`` or ``contract`` field even though one would be convenient:
    accepting either would let a caller redirect an opt-out onto somebody else's
    contract. The number is the only handle, and it is looked up, never trusted.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    sender: str = Field(min_length=1, max_length=64)
    text: str = Field(default="", max_length=1000, alias="message")
    sms_id: str | None = Field(default=None, max_length=128, alias="smsId")
    #: Parsed and logged only. Not used to gate anything: a delivery receipt carries
    #: an event name and no ``message``, which lands on ``ignored`` by itself, so a
    #: second check on a field whose exact spelling is the vendor's would only add a
    #: way to reject a real opt-out.
    webhook_event: str | None = Field(default=None, max_length=64, alias="webhookEvent")


def is_stop_word(text: str) -> bool:
    """Whether ``text`` is a bare opt-out keyword.

    Collapses whitespace and strips trailing punctuation so ``"STOP."`` — which is what
    a phone keyboard produces — is honoured, and lowercases because the keyword is not
    case-sensitive to any carrier. Everything else is a human sentence and is ignored.
    """
    cleaned = " ".join(text.split()).strip(" .!?,;:'\"").lower()
    return cleaned in STOP_WORDS


def valid_signature(settings: Settings, body: bytes, signature: str | None) -> bool:
    """Whether ``signature`` is this deployment's HMAC over ``body``.

    Fails closed on every axis: no secret configured, no header, or a mismatch all
    answer ``False``. An unset secret must never mean "unsigned is fine" — that is the
    shape of bug that makes an open endpoint look authenticated.
    """
    secret = settings.inbound_webhook_secret
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()
    # Both sides are compared as bytes. ``compare_digest`` raises ``TypeError`` on a
    # ``str`` containing a non-ASCII character, and an HTTP header is decoded as
    # latin-1 — so a single byte above 0x7f in ``X-Signature`` would otherwise turn a
    # forged signature into an unhandled 500 for an unauthenticated caller, which is a
    # far cheaper way to fill the log than guessing the digest. Encoding cannot fail
    # here (every ``str`` encodes to UTF-8) and a non-hex byte simply mismatches.
    return hmac.compare_digest(expected.encode("ascii"), signature.strip().lower().encode("utf-8"))


def _accepted() -> JSONResponse:
    """The single response every accepted webhook gets, whatever really happened."""
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content={"status": "accepted"})


async def _raw_body(request: Request) -> bytes:
    """Read the body before parsing, because the signature covers the exact bytes.

    This is the only ``await`` on the path. FastAPI resolves dependencies before it
    calls the handler, so the handler below can stay synchronous while the body is
    still read asynchronously.
    """
    return await request.body()


def _opt_out_one(
    session: Session,
    contract: Contract,
    *,
    correlation_id: str,
    reason: str,
) -> bool:
    """Suppress one contract, audit it, and report whether anything changed.

    ``apply_consent`` returns ``False`` when the four flags already held these values,
    and in that case nothing is written at all — no audit row, no consent timestamp
    move. That is where replay safety lives: a webhook delivered twice (or five times,
    which gateways do when a 202 is slow) applies the same state once and the second
    delivery is a genuine no-op rather than a duplicate audit entry.
    """
    before = consent_flags(contract)
    changed = apply_consent(
        contract,
        consent_email=False,
        consent_sms=False,
        consent_voice=False,
        do_not_contact=True,
    )
    if not changed:
        return False

    AuditLogRepository(session, contract.tenant_id).record(
        correlation_id=correlation_id,
        actor_type=ACTOR_CUSTOMER,
        # No id: the customer is not a user of this deployment. ``ACTOR_CUSTOMER``
        # exists so that "did they withdraw consent, or did an operator on their
        # behalf?" stays answerable; inventing an id here would fabricate the link.
        actor_id=None,
        action=ACTION_CONSENT_OPTED_OUT,
        entity=AUDIT_ENTITY_CONTRACT,
        entity_id=str(contract.id),
        before=before,
        after=consent_flags(contract),
        reason=reason,
    )
    return True


def receive_sms(
    body: bytes = Depends(_raw_body),
    settings: Settings = Depends(app_settings),
    session: Session = Depends(get_request_session),
    contact_index: ContactBlindIndex = Depends(blind_index_or_503),
    signature: Annotated[str | None, Header(alias=SIGNATURE_HEADER)] = None,
) -> JSONResponse:
    """Accept one inbound message and, if it is a STOP, withdraw consent.

    A plain ``def`` on purpose: the handler does blocking database work, so FastAPI
    runs it in a worker thread, while the only ``await`` in the path (reading the body)
    lives in :func:`_raw_body`. An ``async`` handler calling this repository code
    directly would block the event loop for every other request.
    """
    # With no gateway configured there is no such endpoint. 404 rather than 403: a 403
    # would confirm to a scanner that the route exists and is merely switched off.
    if settings.inbound_transport == "none":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    if len(body) > MAX_WEBHOOK_BYTES:
        SMS_INBOUND_TOTAL.inc(outcome=OUTCOME_REJECTED)
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Webhook payload is too large",
        )

    if not valid_signature(settings, body, signature):
        # Counted before raising because a non-zero rate here is one of two things the
        # operator has to be able to see: somebody probing, or a gateway whose shared
        # secret no longer matches ours. Both are otherwise invisible - the caller
        # only ever learns "401".
        SMS_INBOUND_TOTAL.inc(outcome=OUTCOME_REJECTED)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        )

    try:
        message = WebhookMessage.model_validate_json(body)
    except ValidationError:
        SMS_INBOUND_TOTAL.inc(outcome=OUTCOME_MALFORMED)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook payload was not a message",
        ) from None

    if not is_stop_word(message.text):
        SMS_INBOUND_TOTAL.inc(outcome=OUTCOME_IGNORED)
        return _accepted()

    try:
        normalized = normalize_value(FieldName.PHONE, message.sender)
    except ValueError:
        # A number we could not canonicalise can never match a stored one: the digest
        # is taken over the normalized form on both sides. Counted, because a run of
        # these means the gateway is sending numbers in a shape the extractor never
        # produces, and every one of those is an opt-out we did not honour.
        SMS_INBOUND_TOTAL.inc(outcome=OUTCOME_UNMATCHED)
        log.warning("sms_inbound_sender_not_a_phone_number", sms_id=message.sms_id)
        return _accepted()

    digest = contact_digest(contact_index, FieldName.PHONE, normalized)
    if digest is None:
        # Only reachable if PHONE leaves ``CONTACT_FIELD_NAMES``: the write side would
        # then store no index, so no reply could ever match. Logged at error rather
        # than raised, because a 500 makes the gateway retry a request that cannot
        # start succeeding; the operator needs the signal, not a retry storm.
        log.error("sms_inbound_opt_out_not_indexable", field=FieldName.PHONE.value)
        SMS_INBOUND_TOTAL.inc(outcome=OUTCOME_UNMATCHED)
        return _accepted()

    matches = contracts_for_contact(
        session,
        field_name=FieldName.PHONE,
        # The digest, not the number: the stored column is a digest, and the plaintext
        # never has to reach a WHERE clause.
        blind_index=digest,
    )

    correlation_id = correlation_id_or_new()
    suppressed = 0
    for contract in matches:
        if _opt_out_one(
            session,
            contract,
            correlation_id=correlation_id,
            reason="inbound SMS opt-out",
        ):
            suppressed += 1

    if suppressed:
        # One transaction for every contract that changed. Opting out of a number that
        # appears on several contracts (a shared office line, or the same person as a
        # customer of two tenants) is one act, and a partial application would leave
        # some contracts still mailable after the customer said stop.
        session.commit()

    if not matches:
        SMS_INBOUND_TOTAL.inc(outcome=OUTCOME_UNMATCHED)
        # No row is written for an unmatched number: see the module docstring.
        log.info("sms_inbound_no_matching_contract", sms_id=message.sms_id)
    elif suppressed:
        SMS_INBOUND_TOTAL.inc(outcome=OUTCOME_OPTED_OUT)
        log.info(
            "sms_inbound_opted_out",
            sms_id=message.sms_id,
            contracts=suppressed,
            correlation_id=correlation_id,
        )
    else:
        SMS_INBOUND_TOTAL.inc(outcome=OUTCOME_ALREADY_OPTED_OUT)
        log.info("sms_inbound_already_opted_out", sms_id=message.sms_id)

    return _accepted()


router.add_api_route(
    "/sms",
    receive_sms,
    methods=["POST"],
    status_code=status.HTTP_202_ACCEPTED,
    summary="Receive an inbound SMS and honour an opt-out",
    responses={
        status.HTTP_202_ACCEPTED: {"description": "Accepted, whatever the outcome"},
        status.HTTP_400_BAD_REQUEST: {"description": "Payload was not a message"},
        status.HTTP_401_UNAUTHORIZED: {"description": "Signature missing or wrong"},
        status.HTTP_404_NOT_FOUND: {"description": "No inbound transport configured"},
        status.HTTP_413_CONTENT_TOO_LARGE: {"description": "Body too large"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Contact index unavailable"},
    },
)
