"""Extraction backends (F7).

:class:`~app.workers.extraction.DocumentExtractor` is the seam; this module holds
the two things that can sit behind it.

``pattern`` (default)
    Deterministic, offline, labelled regexes. It is the default because it is the
    only backend whose behaviour can be *proven* in this repository — there is no
    Ollama service in ``docker-compose.yml``, so an LLM-only stage would be an
    unverifiable milestone. It reads all seven field types, but not with equal
    evidence: the contact channels and the dates are recognised by *shape*, while
    an address and a date of birth are read only when a label named them at the
    start of a line. Gap 17 was the price of the earlier rule, which read neither
    of them at all (D10-106).

``ollama``
    The local model, opt-in via ``EXTRACTION_BACKEND=ollama``, for the documents a
    labelled regex cannot read — an address written without a label, a value on the
    line below its label, a field no corpus here has shown. "Local only" is
    structural rather than a matter of policy: ``ExtractionBackend`` has no cloud
    member, so a deployment cannot name one (DECISIONS.md D7-02).

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
#:
#: The verb forms are here because the golden corpus
#: (``tests/golden/extraction``) measured them missing: "this agreement **starts
#: on** 2025-02-01" and "the subscription **commences** 14/03/2025" are the shapes
#: contracts are actually written in, and a label set of nouns only found 5 of 8
#: start dates. Every form is anchored — ``end(?:s|ing|ed)``, not ``end\w*`` — so
#: "endorsement" and "end-user" do not become end-date labels.
_START_LABELS = re.compile(
    r"(?i)\b(?:start(?:s|ing|ed)?|commenc(?:es|ing|ed|ement)|begin(?:s|ning)?|effective)"
    r"\s*(?:date|on|from|at)?\b"
)
_END_LABELS = re.compile(
    r"(?i)\b(?:end(?:s|ing|ed)?|expir(?:y|es|ing|ed|ation)|renewal|termination|matur\w*)"
    r"\s*(?:date|on|at)?\b"
    r"|\bvalid\s+(?:until|to)\b"
)
_EMAIL_LABELS = re.compile(r"(?i)\be-?mail\b")
_PHONE_LABELS = re.compile(r"(?i)\b(?:phone|tel|telephone|mobile|cell)\b")

#: Address and date-of-birth labels, anchored to the **start of a line**, because
#: for these two fields the label is the only evidence of where a value begins and
#: the line break is the only evidence of where it ends: "14 Fenwick Row, Leeds LS1
#: 4AB" is a sequence of ordinary words and numbers, and no pattern of *its shape*
#: separates it from a sentence. Three things keep the rule out of trouble and they
#: overlap on purpose: the **colon** is what makes prose ("write to our registered
#: address in Bristol") a sentence rather than a field; the **anchor** stops "Tenant
#: email address:" from offering "address" as the label of a line about an email;
#: and the ``@`` guard in ``_find_address`` refuses a mail address even when a line
#: does read "Email address: a.rahman@example.test".
_ADDRESS_LABELS = re.compile(
    r"(?im)^[ \t]*(?:(?:postal|site|registered|delivery|billing|contact)[ \t]+)?"
    r"(?:address|property|premises)[ \t]*:[ \t]*(\S[^\n]*)$"
)
_DOB_LABELS = re.compile(
    r"(?im)^[ \t]*(?:date[ \t]+of[ \t]+birth|d[ \t]*\.[ \t]*o[ \t]*\.[ \t]*b[ \t]*\.)"
    r"[ \t]*:[ \t]*(\S[^\n]*)$"
)

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"\+?\d[\d\s().\-]{6,}\d")
#: A phone-shaped run of digits can also be a date or an amount. Those are cheap
#: to exclude and expensive to get wrong: a date in the phone column would be a
#: contact attempt to a nonexistent number.
_DATE_LIKE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$|^\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}$")
_AMOUNT_LIKE = re.compile(r"^\d{1,3}(?:[,\s]\d{3})+(?:\.\d{1,2})?$|^\d+\.\d{1,2}$")

#: ``rent`` joined this list the same way: a lease is a contract, the money it is
#: about is the rent, and the golden corpus scored the field 7/8 without it. The
#: neighbouring "service charge" is deliberately *not* a label — it is a second
#: amount beside the value, and a regex that grabs whichever comes first would be
#: picking by document layout.
_VALUE = re.compile(
    r"(?i)\b(?:contract|total|annual|monthly)\s+value\b"
    r"|\b(?:value|fee|fees|price|amount|rent)\b"
)

_DMY = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: The two shapes a date is written in, named once because three finders now look
#: for a date. A second copy of this alternation is how one format silently stops
#: being read while the other keeps scoring.
_DATE_SHAPE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{4}")

#: A phone number worth auto-detecting: 9-15 digits after separators are removed.
_MIN_PHONE_DIGITS = 9
_MAX_PHONE_DIGITS = 15
#: Shortest thing accepted as a postal address. A label is good evidence but not
#: proof: "Address: N/A" and "Address: -" are lines a form leaves behind, and a
#: four-character row in the address column is worse than no row at all.
_MIN_ADDRESS_CHARS = 8
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


def _find_address(text: str) -> list[FieldCandidate]:
    """The rest of a labelled line, when the label says it is a postal address."""
    found: list[FieldCandidate] = []
    for match in _ADDRESS_LABELS.finditer(text):
        value = match.group(1).strip()
        # "Email address: a.rahman@example.test" is the one shape where the label
        # misdescribes what follows it, so an ``@`` disqualifies the value outright
        # rather than being scored lower: the email rule already reads that line at
        # a higher confidence, and a second row holding the same string as an
        # address is a field no reviewer can act on.
        if "@" in value or len(value) < _MIN_ADDRESS_CHARS:
            continue
        found.append(
            FieldCandidate(
                field_name=FieldName.ADDRESS,
                value=value,
                # Below a labelled email's 0.95 on purpose. Both ends of an address
                # are inferred — the label starts it, a line break ends it — so a
                # trailing clause on the same line ("..., billing to the number
                # overleaf") is captured along with the address.
                confidence=0.9,
                source_line=_line_of(text, match.start(1)),
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
            date_match = _DATE_SHAPE.search(tail)
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


def _find_dob(text: str) -> list[FieldCandidate]:
    """A labelled date of birth, in the same two shapes the other dates use."""
    found: list[FieldCandidate] = []
    for match in _DOB_LABELS.finditer(text):
        date_match = _DATE_SHAPE.search(match.group(1))
        if date_match is None:
            continue
        iso = _to_iso(date_match)
        if iso is None:
            # "Date of birth: 03/04/1979" is a month of a person's life, not a
            # typo, so it is dropped for the same reason an ambiguous ``end_date``
            # is: a guessed birthday is worse than a blank one.
            continue
        found.append(
            FieldCandidate(
                field_name=FieldName.DOB,
                value=iso,
                confidence=0.95 if _ISO.match(date_match.group(0)) else 0.85,
                source_line=_line_of(text, match.start(1) + date_match.start()),
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
            *_find_address(text),
            *_find_dates(text),
            *_find_dob(text),
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
