"""Extraction backends (F7).

:class:`~app.workers.extraction.DocumentExtractor` is the seam; this module holds
the two things that can sit behind it.

``pattern`` (default)
    Deterministic, offline, labelled regexes. It is the default because it is the
    only backend whose behaviour can be *proven* in this repository — there is no
    Ollama service in ``docker-compose.yml``, so an LLM-only stage would be an
    unverifiable milestone. It covers the fields that actually drive the product:
    the two contact channels, the two dates the daily scan compares, and the
    contract value.

``ollama``
    The local model, opt-in via ``EXTRACTION_BACKEND=ollama``, for the fields a
    regex cannot reach (``address``, ``dob``). "Local only" is structural rather
    than a matter of policy: ``ExtractionBackend`` has no cloud member, so a
    deployment cannot name one (DECISIONS.md D7-02).

The two are **alternatives, not layers.** Merging a deterministic result with a
model result needs a precedence rule per field, and inventing that rule before
either backend has run against a real document would be guessing (D7-03).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, cast

from app.core.config import Settings
from app.db.enums import FieldName
from app.workers.extraction import (
    DocumentExtractor,
    ExtractionUnavailableError,
    FieldCandidate,
)

__all__ = ["OllamaExtractor", "PatternExtractor", "build_extractor"]

#: Confidence assigned to an LLM value that carries no score of its own.
#: Deliberately below the default review threshold: a value produced by a model
#: has not been checked by anything, so it starts life needing a human.
DEFAULT_MODEL_CONFIDENCE = 0.6

#: Label windows: a value next to one of these words is trusted more than a bare
#: match, because the label is what says *which* date it is.
_START_LABELS = re.compile(r"(?i)\b(?:start(?:ing)?|commencement|effective)\s*(?:date|from)?\b")
_END_LABELS = re.compile(
    r"(?i)\b(?:end|expiry|expiration|renewal|termination|matur\w*)\s*date\b"
    r"|\bvalid\s+until\b"
)
_EMAIL_LABELS = re.compile(r"(?i)\be-?mail\b")
_PHONE_LABELS = re.compile(r"(?i)\b(?:phone|tel|telephone|mobile|cell)\b")

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"\+?\d[\d\s().\-]{6,}\d")
#: A phone-shaped run of digits can also be a date or an amount. Those are cheap
#: to exclude and expensive to get wrong: a date in the phone column would be a
#: contact attempt to a nonexistent number.
_DATE_LIKE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$|^\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}$")
_AMOUNT_LIKE = re.compile(r"^\d{1,3}(?:[,\s]\d{3})+(?:\.\d{1,2})?$|^\d+\.\d{1,2}$")

_VALUE = re.compile(
    r"(?i)\b(?:contract|total|annual|monthly)\s+value\b"
    r"|\b(?:value|fee|fees|price|amount)\b"
)

_DMY = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: A phone number worth auto-detecting: 9-15 digits after separators are removed.
_MIN_PHONE_DIGITS = 9
_MAX_PHONE_DIGITS = 15
#: How far back to look for the label that qualifies a bare match.
_LABEL_WINDOW = 32


def _line_of(text: str, offset: int) -> int:
    """1-based line number of ``offset``, for provenance."""
    return text.count("\n", 0, offset) + 1


def _has_label(text: str, offset: int, labels: re.Pattern[str]) -> bool:
    """Whether a qualifying label appears just before ``offset``."""
    return bool(labels.search(text[max(0, offset - _LABEL_WINDOW) : offset]))


def _find_email(text: str) -> list[FieldCandidate]:
    found: list[FieldCandidate] = []
    for match in _EMAIL.finditer(text):
        labelled = _has_label(text, match.start(), _EMAIL_LABELS)
        found.append(
            FieldCandidate(
                field_name=FieldName.EMAIL,
                value=match.group(0),
                # A bare email is still unambiguous; the label only confirms it is
                # the contract's contact address rather than a signature line.
                confidence=0.95 if labelled else 0.85,
                source_line=_line_of(text, match.start()),
            )
        )
    return found


def _find_phone(text: str) -> list[FieldCandidate]:
    found: list[FieldCandidate] = []
    for match in _PHONE.finditer(text):
        raw = match.group(0).strip()
        if _DATE_LIKE.match(raw) or _AMOUNT_LIKE.match(raw):
            continue
        digits = re.sub(r"\D", "", raw)
        if not _MIN_PHONE_DIGITS <= len(digits) <= _MAX_PHONE_DIGITS:
            continue
        labelled = _has_label(text, match.start(), _PHONE_LABELS)
        found.append(
            FieldCandidate(
                field_name=FieldName.PHONE,
                value=raw,
                # Below the default threshold when unlabelled: a bare number is
                # the shape of a phone number, not evidence that it is one.
                confidence=0.9 if labelled else 0.75,
                source_line=_line_of(text, match.start()),
            )
        )
    return found


def _to_iso(match: re.Match[str]) -> str | None:
    """ISO form of a date match, or ``None`` when the document is ambiguous.

    ``03/04/2025`` is both 3 April and 4 March. Rather than pick one and score it
    confidently, an ambiguous value is dropped: a wrong ``end_date`` silently
    sends a renewal reminder on the wrong day, which is the one failure this
    product exists to prevent (STATUS.md, known gaps).
    """
    raw = match.group(0)
    if _ISO.match(raw):
        return raw
    parts = _DMY.match(raw)
    if parts is None:
        return None
    first, second, year = (int(part) for part in parts.groups())
    if first > 12 and second <= 12:
        day, month = first, second
    elif second > 12 and first <= 12:
        month, day = first, second
    else:
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _find_dates(text: str) -> list[FieldCandidate]:
    found: list[FieldCandidate] = []
    for field_name, labels in (
        (FieldName.START_DATE, _START_LABELS),
        (FieldName.END_DATE, _END_LABELS),
    ):
        for match in labels.finditer(text):
            # The date is what follows the label on the same line.
            tail = text[match.end() : match.end() + 40]
            date_match = re.search(r"\d{4}-\d{2}-\d{2}|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{4}", tail)
            if date_match is None:
                continue
            iso = _to_iso(date_match)
            if iso is None:
                continue
            offset = match.end() + date_match.start()
            found.append(
                FieldCandidate(
                    field_name=field_name,
                    value=iso,
                    # A labelled ISO date is the best case; a labelled
                    # day-first European date is good but not equal to it.
                    confidence=0.95 if _ISO.match(date_match.group(0)) else 0.85,
                    source_line=_line_of(text, offset),
                )
            )
    return found


def _find_contract_value(text: str) -> list[FieldCandidate]:
    found: list[FieldCandidate] = []
    for label in _VALUE.finditer(text):
        tail = text[label.end() : label.end() + 40]
        amount = re.search(
            r"([$£€])?\s*(\d{1,3}(?:[,\s]\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)", tail
        )
        if amount is None:
            continue
        currency, digits = amount.group(1), amount.group(2)
        found.append(
            FieldCandidate(
                field_name=FieldName.CONTRACT_VALUE,
                value=digits,
                # Without a currency symbol the number may be a page reference or
                # a clause count, so it is scored lower.
                confidence=0.9 if currency else 0.8,
                source_line=_line_of(text, label.end() + amount.start()),
            )
        )
    return found


class PatternExtractor:
    """Deterministic, offline extraction from labelled text.

    No network, no model, no clock: the same document always yields the same
    candidates, which is what makes the confidence gate in
    :func:`app.workers.extraction.extract_contract` testable.
    """

    def extract(self, text: str) -> list[FieldCandidate]:
        return [
            *_find_email(text),
            *_find_phone(text),
            *_find_dates(text),
            *_find_contract_value(text),
        ]


_PROMPT = """You extract fields from a contract document.

Reply with ONE JSON object and nothing else. Use only these keys, and omit a key
the document does not state:

email, phone, address, dob, start_date, end_date, contract_value

Rules:
- dates as ISO 8601 (YYYY-MM-DD)
- contract_value as digits only, without a currency symbol
- no commentary, no markdown fences
"""


def _value_and_confidence(raw: Any) -> tuple[str, float]:
    """Accept either a bare value or ``{"value": ..., "confidence": ...}``."""
    if isinstance(raw, dict):
        payload = cast("dict[str, Any]", raw)
        score = payload.get("confidence", DEFAULT_MODEL_CONFIDENCE)
        value = payload.get("value")
    else:
        score = DEFAULT_MODEL_CONFIDENCE
        value = raw
    if not isinstance(value, str) or not value.strip():
        return "", DEFAULT_MODEL_CONFIDENCE
    try:
        confidence = float(score)
    except (TypeError, ValueError):
        confidence = DEFAULT_MODEL_CONFIDENCE
    return value.strip(), min(max(confidence, 0.0), 1.0)


def _candidates_from_payload(payload: Any) -> list[FieldCandidate]:
    """Turn the model's JSON into candidates, ignoring anything unexpected.

    A model that invents a key we do not store — ``tenant_id``, ``status`` — is
    ignored rather than trusted: extraction proposes field *values*, never a row
    or a tenant.
    """
    if not isinstance(payload, dict):
        raise ExtractionUnavailableError("the local model did not return a JSON object")
    body: Any = payload.get("fields", payload)
    if not isinstance(body, dict):
        raise ExtractionUnavailableError("the local model returned an unusable field map")

    found: list[FieldCandidate] = []
    for key, raw in cast("dict[str, Any]", body).items():
        try:
            field_name = FieldName(str(key).strip().lower())
        except ValueError:
            continue
        value, confidence = _value_and_confidence(raw)
        if not value:
            continue
        found.append(FieldCandidate(field_name=field_name, value=value, confidence=confidence))
    return found


class OllamaExtractor:
    """Extraction through a model served by Ollama on this machine.

    Uses ``/api/generate`` with ``format: "json"`` and ``temperature: 0``: the
    first asks Ollama to constrain the output to JSON, the second removes sampling
    noise so re-running extraction on the same document is reproducible.

    ``urllib`` rather than a new dependency — the call is one JSON POST, and the
    runtime requirements are deliberately short and pinned
    (``backend/requirements.txt``).
    """

    def __init__(self, *, base_url: str, model: str, timeout_seconds: float) -> None:
        self._url = f"{base_url.rstrip('/')}/api/generate"
        self._model = model
        self._timeout = timeout_seconds

    def extract(self, text: str) -> list[FieldCandidate]:
        """Send the document text to the local model and parse its answer.

        The text is PII-bearing by definition — that is what is being extracted —
        which is exactly why this backend is loopback-local and why the request is
        never logged (spec: no raw PII in Ollama payloads off-box, or in logs).
        """
        body = json.dumps(
            {
                "model": self._model,
                "prompt": f"{_PROMPT}\n\nDocument:\n{text}",
                "stream": False,
                "format": "json",
                "options": {"temperature": 0},
            }
        ).encode("utf-8")
        # The scheme is fixed by settings (http/https) and the host is operator
        # configuration, not user input; S310 fires on any non-literal URL.
        request = urllib.request.Request(  # noqa: S310  # nosec B310
            self._url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            # The URL is operator configuration, not user input, and the scheme is
            # fixed to http(s) by settings. Bandit flags any urlopen regardless.
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310  # nosec B310
                raw = response.read()
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            # Never surface the endpoint or the document in the message: the first
            # is infrastructure detail, the second is PII.
            raise ExtractionUnavailableError("the local model did not answer") from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ExtractionUnavailableError("the local model returned malformed JSON") from exc

        if not isinstance(payload, dict):
            raise ExtractionUnavailableError("the local model returned an unexpected response")
        inner: Any = cast("dict[str, Any]", payload).get("response")
        if not isinstance(inner, str):
            raise ExtractionUnavailableError("the local model returned no completion")
        try:
            parsed = json.loads(inner)
        except json.JSONDecodeError as exc:
            raise ExtractionUnavailableError("the local model's completion was not JSON") from exc
        return _candidates_from_payload(parsed)


def build_extractor(settings: Settings) -> DocumentExtractor:
    """Select the backend named by ``EXTRACTION_BACKEND``.

    The body is total over ``ExtractionBackend``; an unknown value cannot reach
    here because settings validation rejects it at start-up.
    """
    if settings.extraction_backend == "ollama":
        return OllamaExtractor(
            base_url=settings.ollama_base_url,
            model=settings.extraction_model,
            timeout_seconds=settings.extraction_timeout_seconds,
        )
    return PatternExtractor()
