"""Tests for the P2-D advisory agent (``app.workers.advisory``).

The rules under test are the ones that make a model's opinion safe to *hold*:

* the feature vector carries no customer identifier, and the prompt wraps it in the
  ``<user_input>`` marker so a hostile completion cannot pose as instructions;
* a malformed answer is retried against a bounded budget and then abandoned, never
  guessed at;
* whatever happens, the row either keeps the deterministic channel with
  ``needs_review=True`` — which is what ``dispatch`` reads to refuse it — or adopts
  a channel that ``permitted_channels`` had already offered. There is no path where
  a model failure becomes a contact, and no email fallback anywhere.

``permitted_channels`` itself (consent, opt-out, approval) is the policy's job and
is covered by ``test_agent_decisions.py``; it is stubbed here so these tests pin the
advisory behaviour rather than re-testing eligibility.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agents.prompts import Prompt
from app.core.config import Settings
from app.db.enums import Channel, RiskLevel
from app.db.models import AgentDecision, Contract, ExtractedField
from app.workers import advisory as advisory_module
from app.workers.advisory import (
    ADVISORY_PROMPT_NAME,
    AGE_BUCKET_WITHHELD,
    AdvisoryFeatures,
    AdvisoryUnavailableError,
    ChannelAdvisory,
    CircuitBreaker,
    Completion,
    advise_decision,
    ask,
    features_for,
)

#: The exact keys the model is allowed to see. Asserted literally so that a future
#: field carrying an identifier cannot be added without this test failing first.
PAYLOAD_KEYS = {
    "age_bucket",
    "contract_value_bucket",
    "days_to_expiry",
    "has_email",
    "has_phone",
    "permitted_channels",
}

TODAY = date(2025, 6, 14)
ENDS = date(2025, 7, 8)  # 24 days after TODAY, as in the demo fixtures


def _field(confidence: float = 0.92) -> ExtractedField:
    """A transient ``ExtractedField`` row — nothing is flushed, so no DB is needed.

    A real row rather than a look-alike double, so stubbing ``permitted_channels``
    (whose signature is ``Mapping[Channel, ExtractedField]``) tells no lie.
    """
    field = ExtractedField()
    field.confidence = confidence
    return field


class _Advisor:
    """A ``ChannelAdvisor`` double that replays canned answers and records calls."""

    model = "test-model"

    def __init__(self, *answers: str | Exception) -> None:
        self.answers: list[str | Exception] = list(answers)
        self.calls: list[dict[str, Any]] = []

    def choose(self, prompt: Prompt, payload: Mapping[str, object]) -> Completion:
        self.calls.append(dict(payload))
        if not self.answers:  # pragma: no cover - a test bug, not a product path
            raise AssertionError("advisor asked for more answers than the test supplied")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return Completion(content=answer, model=self.model, tokens_input=11, tokens_output=7)


def contract(risk: RiskLevel = RiskLevel.LOW) -> Contract:
    """A transient contract row — nothing is flushed, so no DB is needed."""
    row = Contract()
    row.risk = risk
    row.end_date = ENDS
    return row


def decision(channel: Channel = Channel.EMAIL, confidence: float = 0.80) -> AgentDecision:
    """The deterministic policy's row, before any advisory runs."""
    return AgentDecision(
        correlation_id=str(uuid4()),
        contract_id=1,
        tenant_id=uuid4(),
        channel=channel,
        confidence=confidence,
        reasoning="policy picked email",
        prompt_version="channel_policy_v1",
    )


def eligible(*channels: Channel) -> dict[Channel, ExtractedField]:
    return {channel: _field() for channel in channels}


def answer(**overrides: object) -> str:
    body: dict[str, object] = {
        "channel": "sms",
        "confidence": 0.88,
        "reasoning": "customer has a phone and no email consent",
        "needs_review": False,
    }
    body.update(overrides)
    return json.dumps(body)


def permit(monkeypatch: pytest.MonkeyPatch, channels: Sequence[Channel]) -> None:
    """Pin the eligibility the policy would have granted for this contract."""
    granted = {channel: _field() for channel in channels}
    monkeypatch.setattr(advisory_module, "permitted_channels", lambda contract, fields: granted)


def advice_settings(settings: Settings, **overrides: object) -> Settings:
    base: dict[str, object] = {"agent_min_confidence": 0.0, "agent_repair_max": 2}
    base.update(overrides)
    return settings.model_copy(update=base)


# -- the schema the model must satisfy ----------------------------------
def test_the_advisory_schema_refuses_extra_keys() -> None:
    """``extra="forbid"``: a prompt-injected extra key cannot look like success."""
    with pytest.raises(ValidationError):
        ChannelAdvisory.model_validate(
            {"channel": "email", "confidence": 0.9, "reasoning": "ok", "send_now": True}
        )


def test_the_advisory_schema_refuses_unknown_channels() -> None:
    with pytest.raises(ValidationError):
        ChannelAdvisory.model_validate(
            {"channel": "carrier_pigeon", "confidence": 0.9, "reasoning": "ok"}
        )


def test_confidence_outside_zero_to_one_is_refused() -> None:
    with pytest.raises(ValidationError):
        ChannelAdvisory.model_validate(
            {"channel": "email", "confidence": 1.4, "reasoning": "very sure"}
        )


# -- the feature vector (no PII may leave the app) ----------------------
def test_the_payload_has_exactly_six_keys_and_names_the_permitted_set() -> None:
    payload = AdvisoryFeatures(
        days_to_expiry=24, value_band=RiskLevel.LOW, has_email=True, has_phone=False
    ).payload((Channel.EMAIL, Channel.SMS))
    assert set(payload) == PAYLOAD_KEYS
    assert sorted(payload["permitted_channels"]) == ["email", "sms"]  # type: ignore[arg-type]


def test_the_payload_only_carries_coarse_categories() -> None:
    """Every value is a bool, a bucket label or a channel name — never free text."""
    payload = AdvisoryFeatures(
        days_to_expiry=24, value_band=RiskLevel.LOW, has_email=True, has_phone=True
    ).payload((Channel.EMAIL,))
    allowed_strings = {"unknown", "low", "medium", "high", "email", "sms"}
    channels = {"email", "sms", "voice"}
    for name, value in payload.items():
        if isinstance(value, str):
            assert value in allowed_strings, f"{name} carries a free-text value"
        elif isinstance(value, list):
            assert set(value) <= channels, f"{name} carries something other than channels"
        else:
            assert isinstance(value, (bool, int)) or value is None, name


def test_features_withhold_the_age_bucket_by_default() -> None:
    assert features_for(contract(), eligible(Channel.EMAIL), today=TODAY).age_bucket == (
        AGE_BUCKET_WITHHELD
    )


def test_features_read_presence_from_the_permitted_fields_only() -> None:
    features = features_for(contract(), eligible(Channel.EMAIL), today=TODAY)
    assert features.has_email is True
    # A contract with an email but no phone/SMS/voice permission must not tell the
    # model a phone exists: presence is derived from what the policy granted.
    assert features.has_phone is False
    assert features.days_to_expiry == 24


# -- ask(): bounded repair, no guessing ---------------------------------
def test_a_fenced_json_answer_is_accepted() -> None:
    advisor = _Advisor(f"Here you go:\n```json\n{answer()}\n```")
    outcome = ask(advisor, Prompt(name="advisory_channel", version=1, text="sys"), {})
    assert outcome.advisory is not None
    assert outcome.advisory.channel == Channel.SMS
    assert outcome.repairs == 0


def test_a_repair_is_retried_once_then_accepted() -> None:
    advisor = _Advisor("this is not json at all", answer())
    outcome = ask(
        advisor, Prompt(name="advisory_channel", version=1, text="sys"), {}, max_repairs=1
    )
    assert len(advisor.calls) == 2
    assert outcome.repairs == 1
    assert outcome.advisory is not None


def test_exhausted_repairs_report_invalid_and_stop() -> None:
    advisor = _Advisor(*["nope"] * 5)
    outcome = ask(
        advisor, Prompt(name="advisory_channel", version=1, text="sys"), {}, max_repairs=2
    )
    assert len(advisor.calls) == 3  # first attempt + two repairs, then give up
    assert outcome.advisory is None
    assert outcome.failure == "invalid"
    assert outcome.repairs == 2


def test_a_transport_failure_is_reported_as_unavailable() -> None:
    outcome = ask(
        _Advisor(AdvisoryUnavailableError("ollama refused the connection")),
        Prompt(name="advisory_channel", version=1, text="sys"),
        {},
    )
    assert outcome.advisory is None
    assert outcome.failure == "unavailable"
    assert "refused" in outcome.detail


# -- the breaker --------------------------------------------------------
def test_the_breaker_opens_after_three_failures_and_closes_after_a_success() -> None:
    now = [0.0]
    breaker = CircuitBreaker(failure_threshold=3, reset_seconds=60.0, clock=lambda: now[0])
    assert breaker.state == "closed"

    for _ in range(3):
        breaker.record_failure()
    assert breaker.state == "open"
    assert breaker.allow() is False

    now[0] += 61.0
    assert breaker.state == "half_open"
    assert breaker.allow() is True

    breaker.record_success()
    assert breaker.state == "closed"
    assert breaker.failures == 0


# -- advise_decision(): the fail-safe seam ------------------------------
def test_a_permitted_channel_repoints_the_decision_with_provenance(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    permit(monkeypatch, [Channel.EMAIL, Channel.SMS])
    row = decision()
    advisor = _Advisor(answer())

    result = advise_decision(
        row,
        contract(),
        [],
        settings=advice_settings(settings),
        advisor=advisor,
        breaker=CircuitBreaker(),
        today=TODAY,
    )

    assert result is row  # mutated in place: the caller's transaction is unchanged
    assert result.channel == Channel.SMS
    assert result.needs_review is False
    assert result.confidence == pytest.approx(0.92)  # the field's confidence, not the model's
    assert result.prompt_version == f"{ADVISORY_PROMPT_NAME}_v1"
    assert result.llm_model == "test-model"
    assert result.temperature == 0.0
    assert result.tokens_input == 11
    assert result.schema_repair_attempts == 0
    assert result.latency_ms is not None
    assert "customer has a phone" in result.reasoning


def test_low_self_reported_confidence_flags_the_row_for_review(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    permit(monkeypatch, [Channel.EMAIL, Channel.SMS])
    result = advise_decision(
        decision(),
        contract(),
        [],
        settings=advice_settings(settings, agent_min_confidence=0.9),
        advisor=_Advisor(answer(confidence=0.5)),
        breaker=CircuitBreaker(),
        today=TODAY,
    )
    assert result.needs_review is True


def test_an_unpermitted_channel_is_never_adopted(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """A valid object naming an ineligible channel is the injection case."""
    permit(monkeypatch, [Channel.EMAIL])
    result = advise_decision(
        decision(),
        contract(),
        [],
        settings=advice_settings(settings),
        advisor=_Advisor(answer(channel="voice")),
        breaker=CircuitBreaker(),
        today=TODAY,
    )
    assert result.channel == Channel.EMAIL  # the policy's channel is kept
    assert result.needs_review is True
    assert "did not" in result.reasoning


def test_a_model_failure_keeps_the_policy_channel_and_flags_review(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """The spec's fallback: ``needs_review=True`` and no contact — never an email."""
    permit(monkeypatch, [Channel.SMS])
    row = decision(channel=Channel.SMS)
    result = advise_decision(
        row,
        contract(),
        [],
        settings=advice_settings(settings),
        advisor=_Advisor(AdvisoryUnavailableError("timeout")),
        breaker=CircuitBreaker(),
        today=TODAY,
    )
    assert result.channel == Channel.SMS
    assert result.needs_review is True
    assert "advisory skipped" in result.reasoning


def test_an_open_breaker_skips_the_model_entirely(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    permit(monkeypatch, [Channel.EMAIL, Channel.SMS])
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=60.0)
    breaker.record_failure()
    assert breaker.state == "open"

    advisor = _Advisor()
    result = advise_decision(
        decision(),
        contract(),
        [],
        settings=advice_settings(settings),
        advisor=advisor,
        breaker=breaker,
        today=TODAY,
    )
    assert advisor.calls == []  # not even asked
    assert result.channel == Channel.EMAIL
    assert result.needs_review is True
    assert "circuit_open" in result.reasoning


def test_a_full_queue_is_backpressure_and_does_not_trip_the_breaker(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """A saturated semaphore is a local condition, not a verdict on the model."""

    class _Full:
        def acquire(self, timeout: float) -> bool:
            return False

        def release(self) -> None:  # pragma: no cover - never released
            raise AssertionError("a refused permit must not be released")

    permit(monkeypatch, [Channel.EMAIL, Channel.SMS])
    monkeypatch.setattr(advisory_module, "_SLOTS", _Full())
    breaker = CircuitBreaker()
    advisor = _Advisor()

    result = advise_decision(
        decision(),
        contract(),
        [],
        settings=advice_settings(settings),
        advisor=advisor,
        breaker=breaker,
        today=TODAY,
    )

    assert advisor.calls == []
    assert breaker.failures == 0
    assert breaker.state == "closed"
    assert result.needs_review is True
    assert "queue_full" in result.reasoning


def test_the_rendered_prompt_delimits_the_payload() -> None:
    """The payload sits inside the injection marker, and the system text is outside."""
    payload = AdvisoryFeatures(
        days_to_expiry=24, value_band=RiskLevel.LOW, has_email=True, has_phone=False
    ).payload((Channel.EMAIL,))
    rendered = advisory_module._render_prompt(
        Prompt(name="advisory_channel", version=1, text="SYSTEM RULES"), payload
    )
    assert "SYSTEM RULES" in rendered
    assert "<user_input>" in rendered
    assert "</user_input>" in rendered
    inside = rendered.split("<user_input>", 1)[1].split("</user_input>", 1)[0]
    assert json.loads(inside) == payload
