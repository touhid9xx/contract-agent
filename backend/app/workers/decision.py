"""Channel-selection policy (F9).

F8 made ``approved`` reachable; nothing consumed it (gap 15). This module is the
arrow after review: given a contract and its fields, decide *which channel* the
agent would use, and refuse loudly when it may not decide at all.

Three properties this module is built around, each taken from a rule the repo
already states:

1. **It reads ``blocks_pipeline``, it does not re-implement it.** A field that
   holds the pipeline is a hard stop, and there is exactly one definition of that
   (``ExtractedField.blocks_pipeline``). A second copy of the rule here is how the
   two would start disagreeing.
2. **No channel switch that bypasses consent or opt-out.** The candidate set is
   ``contract.consent_channels`` intersected with the channels that have a usable
   value. There is no argument, header or request field that can widen it.
3. **Deterministic, not probabilistic.** There is no prompt here and no model
   call: the choice is the documented priority order applied to the candidate set,
   so every refusal and every selection is reproducible and testable offline. That
   is the same trade-off D7-03 made for extraction, for the same reason — a live
   Ollama has never answered anything in this repo, and a policy that silently
   depended on it would be untestable.

What is deliberately **not** here yet, so it is not mistaken for done:

* **Quiet hours.** The window is a *timing* input, checked immediately before
  sending, not a *channel-selection* input. It needs `Contract.timezone` resolved
  against a tz database (absent at runtime — D6-03) and belongs to the dispatch
  slice (D9-03).
* **Override and flag-review.** Both are operator edits to an existing decision,
  so both live in the transport layer (``app/api/v1/agent.py``), not here. What
  this module provides is :func:`permitted_channels` — the gate set an override
  must respect — because a person choosing a channel chooses *among* the channels
  the rules already allow. If the route re-implemented those gates, the operator
  path and the policy path would eventually disagree about who may be contacted,
  and the disagreement would be invisible until someone was contacted twice.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.db.enums import Channel, ContractStatus, FieldName, FieldStatus
from app.db.models import AgentDecision, Contract, ExtractedField

__all__ = [
    "CHANNEL_CONTACT_FIELDS",
    "CHANNEL_POLICY_VERSION",
    "CHANNEL_PRIORITY",
    "CLOSED_CONTRACT_STATUSES",
    "DETERMINISTIC_DECIDER",
    "REFUSAL_CHANNEL_NOT_PERMITTED",
    "ChannelChoice",
    "DecisionRefusedError",
    "build_decision",
    "permitted_channels",
    "select_channel",
]

#: The documented priority order, most preferred first.
#:
#: Email outranks SMS outranks voice because the channel carries different weight
#: and cost: an email is a written record the customer can re-read, an SMS is
#: short and hard to act on at length, and a phone call is the most intrusive and
#: the most expensive to staff. The order is fixed data, not a score, so a reader
#: can predict the outcome rather than reverse-engineer it.
CHANNEL_PRIORITY: tuple[Channel, ...] = (Channel.EMAIL, Channel.SMS, Channel.VOICE)

#: Which extracted field can satisfy which channel.
#:
#: Two channels share ``PHONE``: a mobile number is what an SMS goes to and what a
#: voice call dials. ``ADDRESS`` is absent because ``Channel`` has no postal
#: member — a postal address is not a contact channel this system can use, so an
#: approved address must not be mistaken for reachability.
CHANNEL_CONTACT_FIELDS: dict[Channel, FieldName] = {
    Channel.EMAIL: FieldName.EMAIL,
    Channel.SMS: FieldName.PHONE,
    Channel.VOICE: FieldName.PHONE,
}

#: Field states whose value may be used to contact a customer.
#:
#: ``PENDING_REVIEW`` and ``REJECTED`` are excluded twice over — once here and once
#: by the ``blocks_pipeline`` gate that runs before this filter — because a value
#: that reached the usable map without being approved would be the exact failure
#: the review gate exists to prevent. ``UNREADABLE`` does not block the pipeline but
#: carries no value either, so it is not usable.
USABLE_FIELD_STATUSES: frozenset[FieldStatus] = frozenset(
    {FieldStatus.APPROVED, FieldStatus.AUTO_ACCEPTED}
)

#: Contract states where a reminder decision no longer makes sense. ``ESCALATED``
#: is deliberately *not* here: an escalated contract is the one most in need of a
#: next attempt, and treating it as closed would silence it.
CLOSED_CONTRACT_STATUSES: frozenset[ContractStatus] = frozenset(
    {ContractStatus.RESPONDED, ContractStatus.RENEWED, ContractStatus.EXPIRED}
)

#: Stored in ``AgentDecision.prompt_version``. A policy version, not a prompt
#: version: the column is NOT NULL and exists so a stored recommendation can be
#: explained by the rules in force when it was made. When the priority order or
#: the candidate rules change, this string changes with them.
CHANNEL_POLICY_VERSION = "policy.channel.1"

#: Stored in ``AgentDecision.llm_model``. Deliberately not a model name: recording
#: ``qwen2:7b`` for a decision no model produced would be a fabricated provenance
#: row in a table whose entire purpose is provenance.
DETERMINISTIC_DECIDER = "deterministic"

# --- machine-readable refusal reasons ---------------------------------------
#: The contract cannot be reminded about at all (responded/renewed/expired).
REFUSAL_CONTRACT_CLOSED = "CONTRACT_CLOSED"
#: A field awaiting review or rejected is holding the pipeline.
REFUSAL_PIPELINE_BLOCKED = "PIPELINE_BLOCKED"
#: ``do_not_contact`` is set: the customer asked for no contact.
REFUSAL_CONTACT_REFUSED = "CONTACT_REFUSED"
#: No per-channel consent has been recorded.
REFUSAL_NO_CONSENT = "NO_CONSENT"
#: Consent exists, but no approved value can satisfy a consented channel.
REFUSAL_NO_CONTACT_VALUE = "NO_CONTACT_VALUE"
#: An operator asked to override *to* a channel this contract cannot support.
#: Distinct from the refusals above, which mean no channel at all is available:
#: this one means the channel that was asked for is not among the permitted ones.
#: An override chooses among permitted channels; it never creates permission.
REFUSAL_CHANNEL_NOT_PERMITTED = "CHANNEL_NOT_PERMITTED"


class DecisionRefusedError(Exception):
    """No channel may be selected for this contract, and here is why.

    A refusal is not a failure of the slice — it is the policy working. Each
    refusal carries a stable ``code`` so a caller (and the Phase-0 UI, which
    already branches on ``PIPELINE_BLOCKED``) can react without parsing prose, and
    optional ``details`` that never contain a field value.
    """

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True, slots=True)
class ChannelChoice:
    """A selected channel and the evidence it rests on."""

    channel: Channel
    #: The field whose approved value makes the channel usable. Named, never
    #: valued: this ends up in a log line and in ``reasoning``.
    field_name: FieldName
    #: The chosen field's extraction confidence — how sure the pipeline is that it
    #: can reach this customer, which is the only confidence that means anything
    #: for a channel choice.
    confidence: float
    #: A fixed, redacted sentence. Never document text, never a value.
    reasoning: str


def _usable_fields(fields: Sequence[ExtractedField]) -> dict[FieldName, ExtractedField]:
    """The fields whose value may be used to contact someone, keyed by name.

    ``normalized_value`` is required as well as the status: a field can be
    ``approved`` with no value at all (an operator marked an empty extraction
    readable), and that is a row that cannot send anything.
    """
    return {
        field.field_name: field
        for field in fields
        if field.status in USABLE_FIELD_STATUSES and field.normalized_value is not None
    }


def _reasoning(
    channel: Channel, consented: set[Channel], eligible: Sequence[tuple[Channel, ExtractedField]]
) -> str:
    """Explain the choice in a sentence that cannot leak PII.

    Only channel names, consent gaps and the priority order appear here. The email
    address or phone number that was chosen is *never* interpolated — this string
    is stored in ``AgentDecision.reasoning``, which is plain text readable by any
    operator, and the values it would describe are encrypted precisely so that
    reading one is an audited action.
    """
    granted = ", ".join(sorted(member.value for member in consented))
    if len(eligible) == 1:
        return (
            f"{channel.value} is the only consented channel with an approved contact value "
            f"(consent: {granted}). Selected by deterministic policy, no model call."
        )
    preferred = ", ".join(member.value for member, _field in eligible)
    order = ", ".join(member.value for member in CHANNEL_PRIORITY)
    return (
        f"{preferred} are consented channels with approved contact values "
        f"(consent: {granted}); {channel.value} wins the documented priority order "
        f"({order}). Selected by deterministic policy, no model call."
    )


def permitted_channels(
    contract: Contract, fields: Sequence[ExtractedField]
) -> dict[Channel, ExtractedField]:
    """Every channel a person may choose, and the field that makes each usable.

    This is the same set the policy chooses *from*. An override picks a member of
    it and nothing else: a person choosing a channel chooses among the channels the
    rules already allow, and never widens them. The function exists so the override
    route cannot re-implement these gates and get one of them subtly wrong — the
    review gate, the ``do_not_contact`` opt-out and the consent veto all run here,
    once, for both the policy and the operator.

    The gates run in a fixed order, and the order is part of the contract with the
    caller because it decides which refusal a caller with several problems at once
    is told about:

    1. **Contract closed** — nothing to remind anyone about.
    2. **Pipeline blocked** — a field is unapproved, so no value may be used.
    3. **``do_not_contact``** — a standing opt-out, which outranks a grant.
    4. **No consent at all** — nothing has been granted for any channel.
    5. **Consent but no usable value** — the customer agreed, the extraction did
       not produce anything to use.

    Steps 1-2 are about the contract's own state; 3-5 are the customer-contact
    policy, ordered from the strongest veto to the weakest reason.

    The result is ordered by :data:`CHANNEL_PRIORITY`, so its first item is the
    channel the deterministic policy itself would pick.
    """
    if contract.status in CLOSED_CONTRACT_STATUSES:
        raise DecisionRefusedError(
            REFUSAL_CONTRACT_CLOSED,
            f"This contract is {contract.status.value}; no reminder decision is made "
            "for a closed contract",
        )

    blocking = [field for field in fields if field.blocks_pipeline]
    if blocking:
        # Field *names* are safe to disclose — they are the same labels the review
        # queue already lists to any viewer. Their values are not, and are absent.
        raise DecisionRefusedError(
            REFUSAL_PIPELINE_BLOCKED,
            "The pipeline is blocked by extracted fields that a person has not approved",
            details={
                "blocking": len(blocking),
                "fields": sorted({field.field_name.value for field in blocking}),
            },
        )

    if contract.do_not_contact:
        raise DecisionRefusedError(
            REFUSAL_CONTACT_REFUSED,
            "The customer has asked not to be contacted",
        )

    consented = contract.consent_channels
    if not consented:
        raise DecisionRefusedError(
            REFUSAL_NO_CONSENT,
            "No channel consent has been recorded for this contract",
        )

    usable = _usable_fields(fields)
    # A plain loop rather than a comprehension with a walrus: the assigned name has
    # to narrow to ``ExtractedField`` for the dict value, and an explicit loop says
    # that without relying on a type checker to follow the assignment expression.
    eligible: dict[Channel, ExtractedField] = {}
    for channel in CHANNEL_PRIORITY:
        if channel not in consented:
            continue
        field = usable.get(CHANNEL_CONTACT_FIELDS[channel])
        if field is not None:
            eligible[channel] = field
    if not eligible:
        raise DecisionRefusedError(
            REFUSAL_NO_CONTACT_VALUE,
            "Consent is recorded, but no approved contact value exists for a consented channel",
            details={
                "consented": sorted(member.value for member in consented),
                "required_fields": sorted(
                    CHANNEL_CONTACT_FIELDS[member].value for member in consented
                ),
            },
        )

    return eligible


def select_channel(contract: Contract, fields: Sequence[ExtractedField]) -> ChannelChoice:
    """Pick the channel this contract should be reminded on, or refuse.

    The gates that decide whether any channel is available at all live in
    :func:`permitted_channels`; this function only applies the documented priority
    order to the survivors, so a channel refused for consent can never win by
    being listed first.
    """
    eligible = permitted_channels(contract, fields)
    channel, field = next(iter(eligible.items()))
    return ChannelChoice(
        channel=channel,
        field_name=field.field_name,
        confidence=float(field.confidence),
        reasoning=_reasoning(channel, contract.consent_channels, list(eligible.items())),
    )


def build_decision(
    contract: Contract, fields: Sequence[ExtractedField], *, correlation_id: str
) -> AgentDecision:
    """Turn a policy choice into the row to persist. Raises like :func:`select_channel`.

    ``needs_review`` is always ``False`` here, and that is a statement about this
    policy rather than an oversight: a deterministic selector cannot be *uncertain*
    — it either finds a permitted channel or raises. Setting the flag to record
    "a model would have had more to say" would make it permanently true and
    therefore meaningless. A model-backed selector, which can genuinely be unsure,
    is the slice that gets to set it (D9-05).
    """
    choice = select_channel(contract, fields)
    return AgentDecision(
        correlation_id=correlation_id,
        contract_id=contract.id,
        channel=choice.channel,
        confidence=choice.confidence,
        reasoning=choice.reasoning,
        needs_review=False,
        prompt_version=CHANNEL_POLICY_VERSION,
        llm_model=DETERMINISTIC_DECIDER,
    )
