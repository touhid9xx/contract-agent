"""Extraction quality, measured against a golden corpus (P2-B).

Everything the product does downstream is gated on one number that had never been
measured: ``extraction_review_threshold`` (default ``0.8``). A candidate at or
above it may skip human review and go straight into the notification pipeline
(``ExtractedField.blocks_pipeline``); a candidate below it is a hard stop. So the
threshold is not a preference, it is the line between "a customer is emailed an
expiry date" and "a person checks the date first" — and it was chosen by reading
the confidence constants in :mod:`app.workers.extractors` and reasoning about
them, not by scoring the extractor against any document.

This module supplies the missing half. Given a *golden corpus* — documents whose
correct field values a person has written down — it computes the standard
precision/recall/F1 per field, and the same three numbers pooled across fields.

What the score is, precisely
----------------------------

It is **not** "how many regexes matched". Two rules from the write path are
applied first, and both can only lower the score:

1. :func:`~app.workers.extraction.best_per_field` — one candidate per field, the
   most confident. A document with two email addresses yields one, the same one
   :func:`~app.workers.extraction.extract_contract` would store.
2. :func:`~app.workers.extraction.normalize_value` — the canonical form. A value
   that cannot be normalized is *discarded* by the write path, so it cannot count
   as a hit here either; it is counted separately as ``discarded`` so a corpus
   full of unusable text is visible rather than silently scored as zero.

Comparison then happens between normalized forms, which is what makes the score
insensitive to formatting in the way the product is: a document saying
``24/06/2025`` and an expectation of ``2025-06-24`` are the same value, because
``normalize_value`` says so and the stored field says so.

What it is not
--------------

The corpus in this repository was written by the same person who wrote the
extractor, so a green gate is **weak evidence about the real world** and strong
evidence about *regressions* — which is what it is for. A regex that starts
matching the wrong date, or a normalization change that orphans a format, moves
this number and fails the gate. An operator measuring their own contracts points
``--golden`` at their own corpus, which is the only way to learn anything about
the documents this deployment actually receives.

No PII leaves this module: the report is counts, and the refusals name a document
label and a field name, never a value or a line of document text.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from app.db.enums import FieldName
from app.workers.extraction import DocumentExtractor, best_per_field, normalize_value

__all__ = [
    "DEFAULT_MIN_F1",
    "EvaluationRefusedError",
    "EvaluationReport",
    "FieldScore",
    "GoldenCorpus",
    "GoldenDocument",
    "load_golden_corpus",
    "score_corpus",
]

#: The phase-2 acceptance gate. Kept here rather than in ``Settings`` on purpose:
#: it is a property of a *corpus*, not of a deployment. A deployment that replaced
#: its documents has a different F1, and demanding a new setting to say so would
#: be inventing configuration for a measurement.
DEFAULT_MIN_F1 = 0.85


class EvaluationRefusedError(RuntimeError):
    """The golden corpus cannot be scored as written.

    Every cause is a mistake in the corpus rather than in the extractor — a
    misspelled field name, an expectation no normalizer accepts, a document path
    pointing outside the corpus directory. Refusing is the point: scoring a corpus
    whose expectations were silently dropped would produce a number that looks
    like a measurement and is not one.
    """


@dataclass(frozen=True, slots=True)
class GoldenDocument:
    """One document and the values a person says it contains.

    ``expected`` holds **normalized** values, so scoring is a plain equality
    against an equally normalized prediction. A field absent from ``expected``
    means the document does not state it — which makes predicting it a false
    positive, not a free pass.
    """

    name: str
    text: str
    expected: Mapping[FieldName, str]


@dataclass(frozen=True, slots=True)
class GoldenCorpus:
    """Every document the measurement runs over."""

    documents: tuple[GoldenDocument, ...]

    @property
    def fields(self) -> tuple[FieldName, ...]:
        """Fields at least one document states, in enum order."""
        return tuple(
            name
            for name in FieldName
            if any(name in document.expected for document in self.documents)
        )


@dataclass(frozen=True, slots=True)
class FieldScore:
    """One field's confusion counts across the corpus.

    ``matched`` is a true positive, ``wrong`` a false positive (a value the
    document does not state, or a different value than it states), ``missed`` a
    false negative. ``expected`` is therefore ``matched + missed`` and
    ``predicted`` is ``matched + wrong``; both are derived rather than stored so
    the three counts can never disagree with each other.
    """

    field_name: FieldName
    matched: int
    wrong: int
    missed: int

    @property
    def expected(self) -> int:
        """Documents that state this field."""
        return self.matched + self.missed

    @property
    def predicted(self) -> int:
        """Documents the extractor produced a value for."""
        return self.matched + self.wrong

    @property
    def considered(self) -> bool:
        """False when the field is neither stated nor predicted: no information."""
        return self.predicted + self.missed > 0

    @property
    def precision(self) -> float:
        """Of the values produced, the share that were right. ``0.0`` if none."""
        return self.matched / self.predicted if self.predicted else 0.0

    @property
    def recall(self) -> float:
        """Of the values stated, the share that were found. ``0.0`` if none."""
        return self.matched / self.expected if self.expected else 0.0

    @property
    def f1(self) -> float:
        """Harmonic mean, and ``0.0`` rather than ``ZeroDivisionError`` at zero.

        Defined as zero because that is the honest reading: a field the corpus
        states and the extractor never finds scores nothing, whether the failure
        was silence or noise.
        """
        total = self.precision + self.recall
        return 2 * self.precision * self.recall / total if total else 0.0

    def as_dict(self) -> dict[str, Any]:
        """A log-safe record of this field. Counts and ratios only."""
        return {
            "field": self.field_name.value,
            "expected": self.expected,
            "predicted": self.predicted,
            "matched": self.matched,
            "wrong": self.wrong,
            "missed": self.missed,
            "precision": round(self.precision, 3),
            "recall": round(self.recall, 3),
            "f1": round(self.f1, 3),
        }


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """The whole measurement: per-field scores plus the pooled headline number.

    The headline is a **micro** average — counts summed over fields, then scored —
    rather than the mean of the per-field F1s. A macro average would let a field
    that appears in one document weigh as much as the email address that appears
    in all of them, which is not what "how good is this extractor" means.
    """

    documents: int
    scores: tuple[FieldScore, ...]
    discarded: int

    @property
    def scored(self) -> tuple[FieldScore, ...]:
        """Fields the corpus says something about, in enum order."""
        return tuple(score for score in self.scores if score.considered)

    @property
    def matched(self) -> int:
        """Total true positives."""
        return sum(score.matched for score in self.scores)

    @property
    def wrong(self) -> int:
        """Total false positives."""
        return sum(score.wrong for score in self.scores)

    @property
    def missed(self) -> int:
        """Total false negatives."""
        return sum(score.missed for score in self.scores)

    @property
    def precision(self) -> float:
        """Pooled precision."""
        predicted = self.matched + self.wrong
        return self.matched / predicted if predicted else 0.0

    @property
    def recall(self) -> float:
        """Pooled recall."""
        expected = self.matched + self.missed
        return self.matched / expected if expected else 0.0

    @property
    def f1(self) -> float:
        """Pooled F1, zero when nothing matched."""
        total = self.precision + self.recall
        return 2 * self.precision * self.recall / total if total else 0.0

    @property
    def unreachable(self) -> tuple[FieldName, ...]:
        """Fields the corpus states and the extractor never once found.

        The number behind STATUS.md gap 17: the pattern backend has no rule for
        ``address`` or ``dob`` at all, so those score a structural zero and no
        amount of corpus size will move them.
        """
        return tuple(
            score.field_name for score in self.scores if score.expected and not score.matched
        )

    def meets(self, min_f1: float) -> bool:
        """Whether the pooled F1 clears the gate."""
        return self.f1 >= min_f1

    def as_dict(self) -> dict[str, Any]:
        """A log-safe summary. Counts, ratios and field names only."""
        return {
            "documents": self.documents,
            "scored_fields": len(self.scored),
            "matched": self.matched,
            "wrong": self.wrong,
            "missed": self.missed,
            "discarded": self.discarded,
            "precision": round(self.precision, 3),
            "recall": round(self.recall, 3),
            "f1": round(self.f1, 3),
            "unreachable": [name.value for name in self.unreachable],
        }


def score_corpus(corpus: GoldenCorpus, extractor: DocumentExtractor) -> EvaluationReport:
    """Score ``extractor`` against every document in ``corpus``.

    Raises :class:`~app.workers.extraction.ExtractionUnavailableError` if the
    backend cannot answer — deliberately not caught here. "The model is down" and
    "this extractor scores 0.4" must not look the same to whoever reads the
    result, which is why the write path raises rather than returning an empty list
    for the same reason.
    """
    counts: dict[FieldName, list[int]] = {name: [0, 0, 0] for name in FieldName}
    discarded = 0

    for document in corpus.documents:
        predicted: dict[FieldName, str] = {}
        for candidate in best_per_field(extractor.extract(document.text)):
            try:
                predicted[candidate.field_name] = normalize_value(
                    candidate.field_name, candidate.value
                )
            except ValueError:
                # The write path drops this candidate, so it is not a prediction.
                # Counted separately: a document whose only value is unparseable
                # would otherwise be indistinguishable from an empty one.
                discarded += 1

        for name in FieldName:
            expected = document.expected.get(name)
            actual = predicted.get(name)
            if expected is not None and actual is not None:
                if expected == actual:
                    counts[name][0] += 1
                else:
                    # Both halves: a wrong value is a false positive *and* a miss.
                    counts[name][1] += 1
                    counts[name][2] += 1
            elif expected is not None:
                counts[name][2] += 1
            elif actual is not None:
                counts[name][1] += 1

    return EvaluationReport(
        documents=len(corpus.documents),
        scores=tuple(
            FieldScore(
                field_name=name,
                matched=counts[name][0],
                wrong=counts[name][1],
                missed=counts[name][2],
            )
            for name in FieldName
        ),
        discarded=discarded,
    )


def _required_str(row: Mapping[str, Any], key: str) -> str:
    """Read a non-empty string from a manifest entry, or refuse."""
    value: Any = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EvaluationRefusedError(f"every document entry needs a non-empty '{key}'")
    return value.strip()


def _confined_path(root: Path, file_name: str, document_name: str) -> Path:
    """Resolve ``file_name`` inside ``root``, or refuse.

    A corpus is operator-supplied data read by a command that runs with the
    deployment's privileges, so a ``file`` of ``../../../etc/passwd`` (or, on
    Windows, a drive-relative ``C:x``) is a path traversal, not a typo. Resolving
    before the containment check also catches a symlink pointing out of the
    directory, which a string check on ``..`` alone would not.
    """
    candidate = Path(file_name)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise EvaluationRefusedError(
            f"{document_name}: {file_name!r} must be a relative path inside the corpus directory"
        )
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise EvaluationRefusedError(
            f"{document_name}: {file_name!r} resolves outside the corpus directory"
        )
    return resolved


def _read_document(root: Path, file_name: str, document_name: str) -> str:
    """Read one document's text as UTF-8, or refuse."""
    path = _confined_path(root, file_name, document_name)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise EvaluationRefusedError(
            f"{document_name}: {file_name!r} is missing or is not UTF-8 text"
        ) from exc
    if not text.strip():
        raise EvaluationRefusedError(f"{document_name}: {file_name!r} is empty")
    return text


def _expected_fields(raw: Any, document_name: str) -> dict[FieldName, str]:
    """Read and normalize one document's expectations, or refuse.

    Normalization happens here rather than at scoring time so the expectation and
    the prediction are compared in the canonical form the database stores — an
    expected ``+44 20 7946 0132`` and a predicted ``+442079460132`` are the same
    number, and comparing the raw strings would score it a miss.

    What that also means is that a manifest has to write dates the way
    :func:`~app.workers.extraction.normalize_value` accepts them, i.e. ISO. A
    ``31/12/2026`` is refused rather than guessed at, and that is the right answer:
    the same string is 31 December in one locale and 12 March in another.

    A value no normalizer accepts is refused at load time rather than at scoring
    time, because it could never match *anything* and would silently depress the
    score forever — a number that looks like a measurement and is not one.
    """
    if not isinstance(raw, dict) or not raw:
        raise EvaluationRefusedError(f"{document_name}: 'expected' must list at least one field")
    mapping = cast("dict[str, Any]", raw)
    expected: dict[FieldName, str] = {}
    for key, value in mapping.items():
        try:
            field_name = FieldName(str(key).strip().lower())
        except ValueError as exc:
            names = ", ".join(name.value for name in FieldName)
            raise EvaluationRefusedError(
                f"{document_name}: {key!r} is not a field this pipeline extracts ({names})"
            ) from exc
        if field_name in expected:
            raise EvaluationRefusedError(f"{document_name}: {field_name.value} is listed twice")
        if not isinstance(value, str) or not value.strip():
            raise EvaluationRefusedError(
                f"{document_name}: the expected {field_name.value} is blank"
            )
        try:
            expected[field_name] = normalize_value(field_name, value)
        except ValueError as exc:
            # The value itself is PII-shaped, so it is not echoed. The document and
            # field names are enough for an operator to find the line.
            raise EvaluationRefusedError(
                f"{document_name}: the expected {field_name.value} cannot be normalized"
            ) from exc
    return expected


def load_golden_corpus(manifest: Path) -> GoldenCorpus:
    """Load ``manifest`` and the documents it lists, or raise.

    The manifest is JSON::

        {
          "documents": [
            {
              "name": "membership-agreement",
              "file": "membership-agreement.txt",
              "expected": {
                "email": "jane.doe@acme.test",
                "end_date": "31/12/2026",
                "contract_value": "120000.00"
              }
            }
          ]
        }

    ``file`` is resolved relative to the manifest's own directory, so a corpus is a
    directory that can be moved or committed as a unit. ``name`` is a label for the
    operator and appears in the log; it is also what a refusal cites, so the
    message identifies a document without quoting it.
    """
    try:
        raw = manifest.read_bytes()
    except OSError as exc:
        raise EvaluationRefusedError(f"cannot read the manifest: {manifest.name}") from exc
    try:
        payload: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationRefusedError(f"the manifest is not UTF-8 JSON: {manifest.name}") from exc

    if not isinstance(payload, dict):
        raise EvaluationRefusedError("the manifest must be a JSON object with a 'documents' list")
    entries: Any = cast("dict[str, Any]", payload).get("documents")
    if not isinstance(entries, list) or not entries:
        raise EvaluationRefusedError("the manifest must list at least one document")

    root = manifest.parent
    documents: list[GoldenDocument] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise EvaluationRefusedError("every document entry must be a JSON object")
        row = cast("dict[str, Any]", entry)
        name = _required_str(row, "name")
        if name in seen:
            raise EvaluationRefusedError(f"two documents are both named {name!r}")
        seen.add(name)
        file_name = _required_str(row, "file")
        documents.append(
            GoldenDocument(
                name=name,
                text=_read_document(root, file_name, name),
                expected=_expected_fields(row.get("expected"), name),
            )
        )

    return GoldenCorpus(documents=tuple(documents))
