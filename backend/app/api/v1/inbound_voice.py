"""Call outcomes: what the dialler learned that ``VOICE_TRANSPORT=ari`` cannot say (P2-F).

``AriVoiceSender`` places a call and records *placed*. Whether a person or an answering
machine picked up happens inside the Stasis application, which ``D10-144`` recorded as
"not in this build" — so until this route, a voice reminder was indistinguishable from a
voice reminder that reached voicemail, and the ``answered``/``machine`` statuses existed
in :class:`~app.db.enums.NotificationStatus` with nothing able to reach them.

There are two ways to close that gap, and this file is the smaller one. Asterisk's own
``AMD()`` already decides human-or-machine in the dialplan, so the dialler posts its
verdict back here instead of this service consuming ARI events, decoding audio and
running Whisper. The alternative was rejected for this slice on cost, not on taste: it
needs a WebSocket client, an always-on event worker and an ASR model in the image, and
none of those are pinned anywhere in this repository (see ``D10-154``).

What this route is, then: the second unauthenticated entry point in the API, and the
only one that writes a *delivery* fact. The properties that make that safe:

1. **Signed with its own secret.** ``VOICE_OUTCOME_SECRET``, not the SMS gateway's.
   They authenticate different callers with different lifetimes, and one leaked gateway
   secret must not also let its holder invent call outcomes. Unset means no such
   endpoint: the route answers ``404``, the same way ``/inbound/sms`` does.
2. **The channel id is the only handle, and it is not a guess.** A provider-issued id
   for a call this deployment placed; a stranger cannot derive one from a phone number,
   and no tenant is named in the payload for the reason
   :class:`~app.api.v1.inbound.WebhookMessage` gives.
3. **The answer never differs.** Matched, unmatched, already recorded, ``NOTSURE`` — all
   ``202 {"status": "accepted"}``. The caller is our own dialler, but the reply is still
   a shape a log reader could mine, and the operator-facing truth belongs in
   ``voice_outcome_total{outcome}``.
4. **``NOTSURE`` writes nothing.** AMD declining to decide is not one of the two
   verdicts. Folding it into ``machine`` would record that we reached voicemail when we
   do not know that, which is the same class of false claim as never recording anything.

Deliberately not done here: no audio, no transcript, no duration, and no audit row. The
row this moves already exists and already carries ``provider_message_id`` and
``sent_at``; the callback changes one column on it, and ``Notification`` is the record.
"""

from __future__ import annotations

from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from app.api.deps import app_settings, get_request_session
from app.api.v1.inbound import (
    MAX_WEBHOOK_BYTES,
    SIGNATURE_HEADER,
    accepted_response,
    hmac_matches,
    raw_body,
)
from app.core.config import Settings
from app.core.metrics import VOICE_OUTCOME_TOTAL
from app.core.middleware import correlation_id_or_new
from app.db.enums import NotificationStatus
from app.db.repositories.notifications import (
    CONTACTED_STATUSES,
    notification_for_provider_message_id,
)

__all__ = ["AMD_VERDICTS", "STATUS_FOR_VERDICT", "router"]

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/voice", tags=["voice"])

#: Outcome labels. A closed vocabulary, because the series is read by an operator and a
#: label built from arbitrary caller input is a metric that can be grown without bound.
#: ``human``/``machine`` are the two verdicts; ``unknown`` is AMD's own refusal to give
#: one; ``unmatched`` is a callback naming a channel id no row holds; the last two are
#: refusals of this route rather than of the call, and mirror ``sms_inbound_total``'s
#: spelling so one dashboard query covers both webhooks.
OUTCOME_HUMAN: Final = "human"
OUTCOME_MACHINE: Final = "machine"
OUTCOME_UNKNOWN: Final = "unknown"
OUTCOME_UNMATCHED: Final = "unmatched"
OUTCOME_REJECTED: Final = "rejected"
OUTCOME_MALFORMED: Final = "malformed"

#: Asterisk's ``${AMDSTATUS}`` tokens, upper-cased, mapped onto :data:`OUTCOME_*`.
#:
#: The dialplan's own spelling is the wire contract rather than a tidied version of it:
#: an operator writing ``AMD_STATUS=$AMDSTATUS`` into their config is the expected
#: configuration, and refusing ``HUMAN`` because this file preferred lower case would
#: lose real verdicts to a naming preference. ``DONTCARE`` is AMD's fourth token and
#: means "no opinion", which is ``unknown``; it does **not** mean machine.
AMD_VERDICTS: Final[dict[str, str]] = {
    "HUMAN": OUTCOME_HUMAN,
    "MACHINE": OUTCOME_MACHINE,
    "NOTSURE": OUTCOME_UNKNOWN,
    "DONTCARE": OUTCOME_UNKNOWN,
}

#: The one column value each verdict writes. ``unknown`` is absent on purpose — see the
#: module docstring — so a lookup miss here is the code refusing to invent a status.
STATUS_FOR_VERDICT: Final[dict[str, NotificationStatus]] = {
    OUTCOME_HUMAN: NotificationStatus.ANSWERED,
    OUTCOME_MACHINE: NotificationStatus.MACHINE,
}


class CallOutcome(BaseModel):
    """The slice of the dialler's payload this route reads.

    ``extra="ignore"`` for the reason the SMS webhook gives: our own dialplan may grow
    fields (a call duration, a hangup cause) and adding one must not break the callback.
    There is no ``status``/``tenant`` field either: the verdict is translated from a
    closed vocabulary rather than trusted as a status name, so a caller cannot write
    ``suppressed`` into a row and silence a queue.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    #: The ARI channel id, echoed back from what our own dial returned. 255 is the
    #: column's width; refusing longer here means an over-long id is a 400 rather than a
    #: silently truncated comparison that matches a different call.
    channel_id: str = Field(min_length=1, max_length=255)
    verdict: str = Field(min_length=1, max_length=16)


def record_outcome(
    body: bytes = Depends(raw_body),
    settings: Settings = Depends(app_settings),
    session: Session = Depends(get_request_session),
    signature: Annotated[str | None, Header(alias=SIGNATURE_HEADER)] = None,
) -> JSONResponse:
    """Accept one call outcome and move the notification it names.

    A plain ``def`` for the reason :func:`~app.api.v1.inbound.receive_sms` gives: the
    handler does blocking database work, so FastAPI runs it in a worker thread while the
    single ``await`` on the path lives in :func:`~app.api.v1.inbound.raw_body`.
    """
    # The secret is the switch: with none configured there is no such endpoint. 404
    # rather than 403, so a scanner learns nothing about a route that is switched off.
    if not settings.voice_outcome_secret:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    if len(body) > MAX_WEBHOOK_BYTES:
        VOICE_OUTCOME_TOTAL.inc(outcome=OUTCOME_REJECTED)
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Webhook payload is too large",
        )

    if not hmac_matches(settings.voice_outcome_secret, body, signature):
        # Counted before raising, for the reason the SMS route counts: a non-zero rate
        # here is either a probe or a dialplan whose secret has drifted from ours, and
        # the caller only ever learns "401". Both are invisible without this series.
        VOICE_OUTCOME_TOTAL.inc(outcome=OUTCOME_REJECTED)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        )

    try:
        outcome = CallOutcome.model_validate_json(body)
    except ValidationError:
        VOICE_OUTCOME_TOTAL.inc(outcome=OUTCOME_MALFORMED)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook payload was not a call outcome",
        ) from None

    verdict = AMD_VERDICTS.get(outcome.verdict.strip().upper())
    if verdict is None:
        # A word outside AMD's vocabulary is a misconfigured dialplan, and it is refused
        # rather than folded into ``unknown``: that would make a typo look like a call
        # AMD could not decide, which is a fact about the call instead of about the
        # configuration that produced it.
        VOICE_OUTCOME_TOTAL.inc(outcome=OUTCOME_MALFORMED)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook payload was not a call outcome",
        )

    target = STATUS_FOR_VERDICT.get(verdict)
    if target is None:
        # AMD declined to decide. Counted so a deployment whose every call reads
        # ``unknown`` can be seen, and written nowhere: see the module docstring.
        VOICE_OUTCOME_TOTAL.inc(outcome=verdict)
        log.info("voice_outcome_undecided", channel_id=outcome.channel_id)
        return accepted_response()

    notification = notification_for_provider_message_id(session, outcome.channel_id)
    if notification is None:
        VOICE_OUTCOME_TOTAL.inc(outcome=OUTCOME_UNMATCHED)
        log.warning("voice_outcome_no_matching_notification", channel_id=outcome.channel_id)
        return accepted_response()

    if notification.status not in CONTACTED_STATUSES:
        # Only a row that was actually sent can carry an id a callback could name, so
        # this is defence rather than a path. Counted as unmatched and left alone: a
        # callback is not evidence that a failed send succeeded.
        VOICE_OUTCOME_TOTAL.inc(outcome=OUTCOME_UNMATCHED)
        log.warning(
            "voice_outcome_for_a_row_that_was_never_sent",
            notification_id=notification.id,
            status=notification.status.value,
        )
        return accepted_response()

    if notification.status is target:
        # Replay. A second identical post changes nothing and writes nothing, so a
        # dialler that retries a slow 202 does not produce a second state change.
        VOICE_OUTCOME_TOTAL.inc(outcome=verdict)
        log.info("voice_outcome_already_recorded", notification_id=notification.id)
        return accepted_response()

    previous = notification.status
    notification.status = target
    session.commit()

    VOICE_OUTCOME_TOTAL.inc(outcome=verdict)
    log.info(
        "voice_outcome_recorded",
        notification_id=notification.id,
        tenant_id=str(notification.tenant_id),
        previous=previous.value,
        status=target.value,
        correlation_id=correlation_id_or_new(),
    )
    return accepted_response()


router.add_api_route(
    "/outcome",
    record_outcome,
    methods=["POST"],
    status_code=status.HTTP_202_ACCEPTED,
    summary="Record whether a placed call reached a person or a machine",
    responses={
        status.HTTP_202_ACCEPTED: {"description": "Accepted, whatever the outcome"},
        status.HTTP_400_BAD_REQUEST: {"description": "Payload was not a call outcome"},
        status.HTTP_401_UNAUTHORIZED: {"description": "Signature missing or wrong"},
        status.HTTP_404_NOT_FOUND: {"description": "No voice outcall configured"},
        status.HTTP_413_CONTENT_TOO_LARGE: {"description": "Body too large"},
    },
)
