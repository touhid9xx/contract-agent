"""Model-backed advisory channel selection (P2-D).

``app.workers.decision`` answers "which channel may we use" and says why it must
answer it without a model: consent, suppression and availability are policy, and
a policy that depends on a sampled token is not a policy. This module adds the
model *on top of* that answer, never instead of it, and it does so under four
constraints that are the whole point of the file:

1. **It runs after the gate, on an already-permitted set.** ``permitted_channels``
   is evaluated first. The model receives that set and may pick only from it; a
   channel outside the set is a rejected answer, not a new permission. Consent is
   never widened by anything written here.
2. **Failure is a review flag, never a fallback channel.** If the model is
   unreachable, slow, unparseable or unsure, the row keeps the deterministic
   channel, sets ``needs_review``, and ``dispatch`` refuses it
   (``REFUSAL_NEEDS_REVIEW``). Nothing here can send an email because SMS failed.
3. **What leaves the process is a fixed six-key feature vector.** No name,
   address, contact value, date of birth, contract text or identifier is in it —
   the payload is built by :meth:`AdvisoryFeatures.payload` from booleans and
   coarse bands only, and a test asserts the exact key set.
4. **The answer is checked twice.** Pydantic for shape (``ChannelAdvisory``,
   which is the schema the spec mandates), then semantics: is this channel in the
   permitted set, and does the row record provenance we can defend.

Deliberately *not* here: MLflow. The provenance columns on ``agent_decisions``
(``prompt_version``, ``llm_model``, ``temperature``, token counts, ``latency_ms``,
``schema_repair_attempts``) carry the full redacted record today, and the row is
the audit store the operator reads. A tracking sink that duplicates them is a
separate, heavier decision than this slice.

The circuit breaker is per process. Three consecutive failures open it for sixty
seconds; replicas each learn that independently, which is the intended failure
mode — one wedged replica stops calling Ollama, and the others keep working.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agents.prompts import Prompt, PromptRegistryError, load_prompt
from app.core.config import Settings
from app.db.base import utc_now
from app.db.enums import Channel, RiskLevel
from app.db.models import AgentDecision, Contract, ExtractedField
from app.workers.decision import DecisionRefusedError, permitted_channels

__all__ = [
    "ADVISORY_PROMPT_NAME",
    "MAX_CONCURRENT_ADVICE",
    "SLOT_WAIT_SECONDS",
    "AdvisoryFeatures",
    "AdvisoryOutcome",
    "AdvisoryUnavailableError",
    "ChannelAdvisor",
    "ChannelAdvisory",
    "CircuitBreaker",
    "Completion",
    "OllamaAdvisor",
    "advise_decision",
    "ask",
    "build_advisor",
    "default_breaker",
    "features_for",
]

logger = structlog.get_logger(__name__)

#: Name in ``app/agents/prompts/registry.json``. The version comes from the
#: registry, so the row's ``prompt_version`` always names text that exists.
ADVISORY_PROMPT_NAME = "advisory_channel"

#: Reported as ``age_bucket`` while no age bucket may be derived. Bucketing needs
#: a decrypted date of birth, and §7 says to omit features rather than send a
#: re-identifying one we do not need: "how long until this contract ends" plus
#: channel availability already answers the question the model is asked.
AGE_BUCKET_WITHHELD = "unknown"

#: ``reasoning`` is bounded so a chatty model cannot turn an audit column into a
#: log sink. The schema allows 500; the stored sentence is shorter.
MAX_REASONING_CHARS = 480
MAX_MODEL_SENTENCE_CHARS = 300

#: Sanitising the model's own sentence is about shape, not trust: the prompt
#: carries no untrusted text, so this is not the injection defence (that is the
#: permitted-set check plus the fact that no document text is ever sent).
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class AdvisoryUnavailableError(RuntimeError):
    """The model could not be asked, or answered nothing usable at all.

    Raised only for transport-level problems (connection, timeout, HTTP status,
    body that is not the API shape). A body that is the API shape but whose
    content is not a ``ChannelAdvisory`` is a *validation* failure, handled by the
    repair loop, because re-asking is the right response to it.
    """


class ChannelAdvisory(BaseModel):
    """The schema from the build spec, unchanged.

    ``extra="forbid"`` is not decoration: a model that adds ``"cc"`` or
    ``"send_now"`` keys is answering a question we did not ask, and silently
    dropping those keys would let a prompt-injection payload look successful.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: Channel
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1, max_length=500)
    needs_review: bool = False


@dataclass(frozen=True, slots=True)
class Completion:
    """One raw answer plus what it cost, before any validation."""

    content: str
    model: str
    tokens_input: int | None = None
    tokens_output: int | None = None


@dataclass(frozen=True, slots=True)
class AdvisoryFeatures:
    """The only customer-derived data that is allowed to leave the process.

    Every member is a boolean, a coarse band, or a number of days. There is no
    field here that could be a name, a contact value or an account identifier,
    which is what makes the payload safe by construction rather than by review.
    """

    days_to_expiry: int | None
    value_band: RiskLevel
    has_email: bool
    has_phone: bool
    age_bucket: str = AGE_BUCKET_WITHHELD

    def payload(self, permitted: Sequence[Channel]) -> dict[str, object]:
        """The JSON object that is wrapped in ``<user_input>`` and sent.

        The key set is fixed and asserted by a test; adding a key here is the
        only way to widen what a local model learns about a customer.
        """
        return {
            "age_bucket": self.age_bucket,
            "has_email": self.has_email,
            "has_phone": self.has_phone,
            "contract_value_bucket": self.value_band.value,
            "days_to_expiry": self.days_to_expiry,
            "permitted_channels": [channel.value for channel in permitted],
        }


@dataclass(frozen=True, slots=True)
class AdvisoryOutcome:
    """What the model said, if it said anything valid, and at what cost."""

    advisory: ChannelAdvisory | None
    repairs: int = 0
    completion: Completion | None = None
    latency_ms: int = 0
    failure: str | None = None
    #: Last shape error, kept for the log line (never for the database).
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.advisory is not None


class CircuitBreaker:
    """Stop asking a wedged model for a while (3 failures -> 60s open).

    ``clock`` is injectable so the expiry can be tested without sleeping. A
    half-open probe is intentionally one probe: the first success closes the
    breaker, and a failure re-opens it for another full window. Concurrent probes
    are not fenced — this guards a latency budget, not correctness, and two extra
    requests during a reset cannot change a channel because every answer is
    re-checked against the permitted set.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        reset_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = max(1, failure_threshold)
        self.reset_seconds = max(0.0, reset_seconds)
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        return "open" if not self._elapsed() else "half_open"

    @property
    def failures(self) -> int:
        return self._failures

    def _elapsed(self) -> bool:
        """Whether an open breaker has served its reset window.

        ``False`` when nothing is open: :attr:`state` and :meth:`allow` both check
        :attr:`_opened_at` first, so this is never the deciding branch, and an
        ``assert`` here would be a runtime check production could disable.
        """
        if self._opened_at is None:
            return False
        return self._clock() - self._opened_at >= self.reset_seconds

    def allow(self) -> bool:
        if self._opened_at is None:
            return True
        return self._elapsed()

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._opened_at is not None or self._failures >= self.failure_threshold:
            self._opened_at = self._clock()


class ChannelAdvisor(Protocol):
    """Anything that can answer the advisory prompt.

    A protocol rather than a base class so tests hand in a three-line double and
    production hands in :class:`OllamaAdvisor`; nothing in this module imports a
    vendor SDK, and the product never reaches a hosted model.
    """

    @property
    def model(self) -> str: ...

    def choose(self, prompt: Prompt, payload: Mapping[str, object]) -> Completion: ...


class OllamaAdvisor:
    """Ollama ``/api/generate`` in JSON mode, with one fallback model.

    Mirrors the extraction client (``app.workers.extractors``) rather than
    inventing a second HTTP style: ``urllib`` because an HTTP client dependency
    for two POSTs is not worth its CVE surface, ``format="json"`` so the runtime
    constrains sampling, and ``temperature=0`` so a re-run explains itself.

    The fallback model is tried only after a *transport* failure. It is not tried
    after a bad answer: a smaller model is not more likely to follow the schema,
    and the repair loop is the right response there.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        fallback_model: str | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._model = model
        self.fallback_model = fallback_model
        self.timeout_seconds = timeout_seconds

    @property
    def model(self) -> str:
        return self._model

    def choose(self, prompt: Prompt, payload: Mapping[str, object]) -> Completion:
        try:
            return self._ask(self._model, prompt, payload)
        except AdvisoryUnavailableError:
            if not self.fallback_model or self.fallback_model == self._model:
                raise
            logger.warning(
                "agent_advisory_model_fallback",
                primary=self._model,
                fallback=self.fallback_model,
            )
            return self._ask(self.fallback_model, prompt, payload)

    # -- internals --------------------------------------------------------
    def _ask(self, model: str, prompt: Prompt, payload: Mapping[str, object]) -> Completion:
        body = {
            "model": model,
            "prompt": _render_prompt(prompt, payload),
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        }
        # The scheme is fixed by settings (http/https) and the host is operator
        # configuration pinned to loopback by ``Settings``, not user input; S310
        # fires on any non-literal URL.
        request = urllib.request.Request(  # noqa: S310  # nosec B310
            f"{self.base_url}/api/generate",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            # As above: operator configuration, fixed scheme. Bandit flags any urlopen.
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310  # nosec B310
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise AdvisoryUnavailableError(
                f"ollama returned HTTP {exc.code} for model {model}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AdvisoryUnavailableError(
                f"ollama unreachable for model {model}: {_failure_name(exc)}"
            ) from exc

        try:
            parsed: Any = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise AdvisoryUnavailableError("ollama returned a non-JSON body") from exc
        content = parsed.get("response") if isinstance(parsed, dict) else None
        if not isinstance(content, str):
            raise AdvisoryUnavailableError("ollama body has no 'response' string")
        return Completion(
            content=content,
            model=model,
            tokens_input=_as_int(parsed.get("prompt_eval_count")),
            tokens_output=_as_int(parsed.get("eval_count")),
        )


def build_advisor(settings: Settings) -> ChannelAdvisor:
    """The advisor the configured backend asks for.

    Only ``AGENT_BACKEND=ollama`` reaches this; the default backend never builds
    one, so a deterministic deployment has no code path to a model at all.
    """
    return OllamaAdvisor(
        base_url=settings.ollama_base_url,
        model=settings.agent_model,
        fallback_model=settings.agent_fallback_model,
        timeout_seconds=settings.agent_timeout_seconds,
    )


@dataclass
class _BreakerRegistry:
    """The process-wide breaker, swappable by tests through the dependency."""

    current: CircuitBreaker = field(default_factory=CircuitBreaker)

    def get(self) -> CircuitBreaker:
        return self.current


_BREAKERS = _BreakerRegistry()


def default_breaker() -> CircuitBreaker:
    """The breaker the API uses unless a dependency override supplies one."""
    return _BREAKERS.get()


#: How many advice calls one process may have in flight (spec: ``semaphore(5)`` per
#: replica). Ollama serves one model out of limited VRAM, but the sharper reason is
#: the database: the calling thread holds a connection for the whole model round trip,
#: so an unbounded fan-out would drain the pool and break endpoints that have nothing
#: to do with channel selection.
MAX_CONCURRENT_ADVICE = 5

#: How long a caller waits for one of those permits before giving up on the model.
#: Deliberately far below the model timeout: a full queue must not be able to turn
#: into a second timeout's worth of latency on top of the first.
SLOT_WAIT_SECONDS = 5.0


class _AdviceSlots:
    """The process-wide permits guarding concurrent model calls."""

    def __init__(self, limit: int = MAX_CONCURRENT_ADVICE) -> None:
        self._semaphore = threading.BoundedSemaphore(limit)

    def acquire(self, timeout: float) -> bool:
        return self._semaphore.acquire(timeout=max(0.0, timeout))

    def release(self) -> None:
        self._semaphore.release()


_SLOTS = _AdviceSlots()


def features_for(
    contract: Contract,
    eligible: Mapping[Channel, ExtractedField],
    *,
    today: date | None = None,
) -> AdvisoryFeatures:
    """Build the payload from the contract and the *permitted* fields only.

    ``contract_value_bucket`` is the risk band, not a monetary value: the model
    does not need the number to rank a channel, and the number is not stored on
    the contract row at all. ``has_phone`` is true when either SMS or voice is
    available, matching the fact the model is actually being asked about.
    """
    day = today or utc_now().date()
    days = (contract.end_date - day).days if contract.end_date else None
    return AdvisoryFeatures(
        days_to_expiry=days,
        value_band=contract.risk,
        has_email=Channel.EMAIL in eligible,
        has_phone=Channel.SMS in eligible or Channel.VOICE in eligible,
    )


def ask(
    advisor: ChannelAdvisor,
    prompt: Prompt,
    payload: Mapping[str, object],
    *,
    max_repairs: int = 3,
) -> AdvisoryOutcome:
    """Ask, validate, and re-ask with the error attached — at most ``max_repairs``.

    A repair re-asks without echoing the model's previous answer: what goes back
    is a description of *which field was wrong*, never the rejected text, so a
    hostile completion cannot be laundered into the next prompt.
    """
    started = time.perf_counter()
    detail = ""
    last_completion: Completion | None = None
    for attempt in range(max(0, max_repairs) + 1):
        try:
            completion = advisor.choose(prompt, payload)
        except AdvisoryUnavailableError as exc:
            return AdvisoryOutcome(
                advisory=None,
                repairs=attempt,
                completion=last_completion,
                latency_ms=_elapsed_ms(started),
                failure="unavailable",
                detail=str(exc),
            )
        last_completion = completion

        try:
            raw = _extract_json_object(completion.content)
            advisory = ChannelAdvisory.model_validate(raw)
        except (ValueError, ValidationError) as exc:
            detail = _describe(exc)
            logger.warning(
                "agent_advisory_schema_repair",
                attempt=attempt + 1,
                model=completion.model,
                detail=detail,
            )
            continue
        return AdvisoryOutcome(
            advisory=advisory,
            repairs=attempt,
            completion=completion,
            latency_ms=_elapsed_ms(started),
        )
    return AdvisoryOutcome(
        advisory=None,
        repairs=max(0, max_repairs),
        completion=last_completion,
        latency_ms=_elapsed_ms(started),
        failure="invalid",
        detail=detail,
    )


def advise_decision(
    decision: AgentDecision,
    contract: Contract,
    fields: Sequence[ExtractedField],
    *,
    settings: Settings,
    advisor: ChannelAdvisor | None = None,
    breaker: CircuitBreaker | None = None,
    today: date | None = None,
) -> AgentDecision:
    """Re-point an already-permitted deterministic decision with the model's pick.

    Returns the *same* row, mutated in place, so the caller's transaction and the
    optimistic-locking story are unchanged. The deterministic decision is the
    input and the fallback: at no point is a channel chosen that
    ``permitted_channels`` did not offer, and every failure path leaves
    ``needs_review`` set so ``dispatch`` refuses the row.
    """
    try:
        eligible = permitted_channels(contract, fields)
    except DecisionRefusedError:  # pragma: no cover - the caller already gated
        return decision
    if not eligible:
        return decision

    permitted = tuple(eligible)
    fallback_channel = decision.channel
    features = features_for(contract, eligible, today=today)
    payload = features.payload(permitted)

    gate = breaker or default_breaker()
    if not gate.allow():
        return _keep_deterministic(
            decision,
            reason="circuit_open",
            detail=f"breaker {gate.state} after {gate.failures} failures",
            prompt_label=None,
        )

    try:
        prompt = load_prompt(ADVISORY_PROMPT_NAME)
    except PromptRegistryError as exc:
        gate.record_failure()
        logger.error("agent_advisory_prompt_unavailable", detail=str(exc))
        return _keep_deterministic(
            decision, reason="prompt_unavailable", detail=str(exc), prompt_label=None
        )

    if not _SLOTS.acquire(SLOT_WAIT_SECONDS):
        # Local backpressure, not a model failure: the breaker is left alone so a
        # burst of operator traffic cannot trip it and suppress advice that would
        # work fine a second later.
        logger.warning(
            "agent_advisory_queue_full",
            contract_id=contract.id,
            tenant_id=str(contract.tenant_id),
            correlation_id=decision.correlation_id,
            limit=MAX_CONCURRENT_ADVICE,
        )
        return _keep_deterministic(
            decision,
            reason="queue_full",
            detail=f"no permit within {SLOT_WAIT_SECONDS:g}s",
            prompt_label=prompt.label,
        )

    try:
        outcome = ask(
            advisor or build_advisor(settings),
            prompt,
            payload,
            max_repairs=settings.agent_repair_max,
        )
    finally:
        _SLOTS.release()

    if outcome.advisory is None:
        gate.record_failure()
        logger.warning(
            "agent_advisory_fallback",
            reason=outcome.failure,
            contract_id=contract.id,
            tenant_id=str(contract.tenant_id),
            correlation_id=decision.correlation_id,
            prompt_version=prompt.label,
        )
        return _keep_deterministic(
            decision,
            reason=outcome.failure or "unavailable",
            detail=outcome.detail,
            prompt_label=prompt.label,
            outcome=outcome,
        )

    gate.record_success()
    advisory = outcome.advisory
    if advisory.channel not in eligible:
        # A valid object naming a channel the policy did not permit. This is the
        # one answer that must never be honoured quietly: it is either a model
        # error or an injection succeeding, and either way the safest reading is
        # that the decision needs a person.
        logger.warning(
            "agent_advisory_unpermitted_channel",
            chosen=advisory.channel.value,
            permitted=[channel.value for channel in permitted],
            contract_id=contract.id,
            tenant_id=str(contract.tenant_id),
            correlation_id=decision.correlation_id,
        )
        decision.needs_review = True
        decision.reasoning = _clamp(
            f"model chose {advisory.channel.value!r}, which the policy did not "
            f"permit (permitted: {_channel_list(permitted)}); {fallback_channel.value} "
            f"kept from the deterministic policy and flagged for review — nothing is "
            f"dispatched until a person clears it. [{prompt.label}]"
        )
        _stamp_provenance(decision, prompt.label, outcome, settings)
        return decision

    decision.channel = advisory.channel
    decision.confidence = float(eligible[advisory.channel].confidence)
    decision.needs_review = advisory.needs_review or (
        advisory.confidence < settings.agent_min_confidence
    )
    decision.reasoning = _compose_reasoning(advisory, permitted, prompt.label)
    _stamp_provenance(decision, prompt.label, outcome, settings)
    return decision


# -- helpers ------------------------------------------------------------
def _render_prompt(prompt: Prompt, payload: Mapping[str, object]) -> str:
    """System text plus the payload wrapped in the spec's injection marker.

    The payload is serialised by *us* from a fixed key set, so the delimiters are
    a second line of defence rather than the only one: no field in
    :class:`AdvisoryFeatures` can carry text a third party wrote.
    """
    return (
        f"{prompt.text.strip()}\n\n"
        "<user_input>\n"
        f"{json.dumps(payload, sort_keys=True)}\n"
        "</user_input>\n"
    )


def _keep_deterministic(
    decision: AgentDecision,
    *,
    reason: str,
    detail: str,
    prompt_label: str | None,
    outcome: AdvisoryOutcome | None = None,
) -> AgentDecision:
    """Fall back to the policy's channel and stop the row at a human.

    ``needs_review`` is the only thing that makes this a safe fallback: it is what
    ``dispatch`` reads to refuse the row (``REFUSAL_NEEDS_REVIEW``). Setting it is
    therefore not a hint — it is the difference between "a person looks" and "the
    customer is contacted by whatever channel happened to be first".
    """
    decision.needs_review = True
    suffix = f" [{prompt_label}]" if prompt_label else ""
    decision.reasoning = _clamp(
        f"advisory skipped ({reason}{f': {detail}' if detail else ''}); "
        f"{decision.channel.value} kept from the deterministic policy and flagged "
        f"for review — nothing is dispatched until a person clears it.{suffix}"
    )
    if outcome is not None:
        _stamp_provenance(decision, prompt_label or "advisory_unavailable", outcome, None)
    return decision


def _stamp_provenance(
    decision: AgentDecision,
    prompt_label: str,
    outcome: AdvisoryOutcome,
    settings: Settings | None,
) -> None:
    """Record what actually produced the row, or say that nothing did.

    ``llm_model`` is the model that answered when one did. On a transport failure
    it is the model that was *asked*, which is what an operator needs to know; on
    a prompt-registry failure nothing was asked and the label says so.
    """
    decision.prompt_version = prompt_label[:50]
    if outcome.completion is not None:
        decision.llm_model = outcome.completion.model[:50]
        decision.tokens_input = outcome.completion.tokens_input
        decision.tokens_output = outcome.completion.tokens_output
    decision.temperature = 0.0
    decision.latency_ms = outcome.latency_ms
    decision.schema_repair_attempts = max(0, outcome.repairs)
    if settings is not None:
        decision.llm_model = decision.llm_model[:50]


def _compose_reasoning(advisory: ChannelAdvisory, permitted: Sequence[Channel], label: str) -> str:
    """The stored sentence: the model's own, bounded, plus fixed provenance.

    The tail is ours, not the model's, so an operator can always see which text
    produced the row and how sure the model said it was.
    """
    sentence = _clamp(advisory.reasoning, MAX_MODEL_SENTENCE_CHARS)
    tail = (
        f"[model {label}, self-reported confidence {advisory.confidence:.2f}, "
        f"permitted: {_channel_list(permitted)}]"
    )
    return _clamp(f"{sentence} {tail}")


def _channel_list(permitted: Sequence[Channel]) -> str:
    return ", ".join(channel.value for channel in permitted) or "none"


def _clamp(text: str, limit: int = MAX_REASONING_CHARS) -> str:
    cleaned = _CONTROL_CHARS.sub(" ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _extract_json_object(content: str) -> dict[str, Any]:
    """Parse the answer, tolerating a code fence or a sentence around it.

    A model that wraps its object in prose has still answered; spending a repair
    round trip on formatting we can read ourselves wastes the customer's latency.
    Anything that is not an object after that is a genuine shape failure.
    """
    text = content.strip()
    try:
        parsed: Any = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object in the answer") from None
        parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("the answer is not a JSON object")
    return parsed


def _failure_name(exc: BaseException) -> str:
    """The name of what went wrong when the call never reached the model.

    ``URLError`` puts the detail in ``.reason``, which is either an exception
    (``socket.gaierror``) or the string the socket layer produced. Naming
    ``.reason`` directly yields ``"str"`` — no use to whoever reads the log — so
    the raised exception is named first and a nested exception's type is appended.
    Only types are reported, never the accompanying text.
    """
    reason = getattr(exc, "reason", None)
    if isinstance(reason, BaseException):
        return f"{type(exc).__name__} ({type(reason).__name__})"
    return type(exc).__name__


def _describe(exc: BaseException) -> str:
    """One short, value-free description of why an answer was rejected.

    ``ValidationError``'s string form echoes ``input_value``; that is model text,
    and we neither store it nor feed it back. Only locations and messages survive.
    """
    if isinstance(exc, ValidationError):
        parts = []
        for error in exc.errors()[:3]:
            where = ".".join(str(part) for part in error.get("loc", ())) or "answer"
            parts.append(f"{where}: {error.get('msg', 'invalid')}")
        return "; ".join(parts)[:200]
    return str(exc)[:200]


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
