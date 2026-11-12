"""Extraction quality, measured instead of assumed (P2-B).

Every other guarantee in this repository is pinned by a test that fails when the
guarantee breaks. Extraction quality was the exception: ``PatternExtractor`` ships
hand-written confidence constants, the review threshold decides which of its values
reach a customer without a person reading them, and nothing had ever produced a
precision or recall figure for either. A regex could stop matching half the corpus
and the suite would stay green.

These tests cover the three things that makes true:

* **The arithmetic.** Precision, recall and F1 are defined once, including the
  awkward cases — a field the corpus never states that the backend predicts anyway,
  a field the backend never produces, and which average the headline uses. Pooled
  counts, not the mean of per-field scores: a field stated in one document must not
  weigh as much as the email in all eight.
* **The corpus refuses to be wrong.** A misspelled field name, an expectation no
  normalizer accepts, a document path outside the directory — each is refused with
  the document and field named, and *not* the value, because expectations are
  PII-shaped by definition. A silently dropped expectation would produce a number
  that looks like a measurement and is not one.
* **The gate has teeth.** The committed corpus must clear 0.85, and the same
  measurement with one channel removed must fall below it. The second half is the
  one that matters: a gate only ever observed green is indistinguishable from a
  constant.

The corpus is eight documents in ``tests/golden/extraction``. It is a regression
net, not an accuracy claim about customers' contracts — it was written by the same
hand as the regexes it scores. Limits are recorded in D10-94.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.cli import EXIT_BELOW_GATE, EXIT_FAILED, EXIT_OK, build_parser
from app.cli import main as cli_main
from app.core.config import Settings
from app.db.enums import FieldName
from app.workers.extraction import FieldCandidate
from app.workers.extraction_eval import (
    DEFAULT_MIN_F1,
    EvaluationRefusedError,
    EvaluationReport,
    FieldScore,
    GoldenCorpus,
    GoldenDocument,
    load_golden_corpus,
    score_corpus,
)
from app.workers.extractors import PatternExtractor

#: The corpus this repository measures itself against.
CORPUS = Path(__file__).parent / "golden" / "extraction" / "manifest.json"


class Answers:
    """A backend that returns a fixed candidate list per document text.

    Keyed by text because a ``GoldenDocument``'s text is what the backend sees, and
    these fixtures use the document name as its text — so a test reads as "for this
    document, answer with these".
    """

    def __init__(self, by_text: Mapping[str, list[FieldCandidate]]) -> None:
        self._by_text = dict(by_text)
        self.seen: list[str] = []

    def extract(self, text: str) -> list[FieldCandidate]:
        self.seen.append(text)
        return list(self._by_text.get(text, []))


def candidate(field_name: FieldName, value: str, confidence: float = 0.9) -> FieldCandidate:
    return FieldCandidate(field_name=field_name, value=value, confidence=confidence)


def make_corpus(*rows: tuple[str, dict[FieldName, str]]) -> GoldenCorpus:
    """A corpus whose documents are named after their own text."""
    return GoldenCorpus(
        documents=tuple(
            GoldenDocument(name=name, text=name, expected=expected) for name, expected in rows
        )
    )


def write_corpus(
    root: Path,
    entries: list[dict[str, object]],
    *,
    document: str = "A contract, in one line.\n",
) -> Path:
    """Write a manifest plus one document and return the manifest's path."""
    (root / "doc.txt").write_text(document, encoding="utf-8")
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({"documents": entries}), encoding="utf-8")
    return manifest


def entry(
    expected: dict[str, str], *, name: str = "one", file: str = "doc.txt"
) -> dict[str, object]:
    return {"name": name, "file": file, "expected": expected}


@pytest.fixture(scope="module")
def corpus() -> GoldenCorpus:
    """The committed corpus, loaded once per module."""
    return load_golden_corpus(CORPUS)


@pytest.fixture(scope="module")
def measured(corpus: GoldenCorpus) -> EvaluationReport:
    """The committed corpus scored by the backend the deployment defaults to."""
    return score_corpus(corpus, PatternExtractor())


def score_of(report: EvaluationReport, field_name: FieldName) -> FieldScore:
    return next(score for score in report.scores if score.field_name is field_name)


# --------------------------------------------------------------------------- #
# The arithmetic
# --------------------------------------------------------------------------- #


def test_a_perfect_run_pools_to_one() -> None:
    corpus = make_corpus(
        ("doc-a", {FieldName.EMAIL: "a@example.test", FieldName.PHONE: "0123456789"}),
    )
    answers = Answers(
        {
            "doc-a": [
                candidate(FieldName.EMAIL, "a@example.test"),
                candidate(FieldName.PHONE, "0123456789"),
            ]
        }
    )

    report = score_corpus(corpus, answers)

    assert (report.matched, report.wrong, report.missed) == (2, 0, 0)
    assert report.f1 == 1.0
    assert report.meets(DEFAULT_MIN_F1)
    assert report.unreachable == ()


def test_a_prediction_the_document_does_not_state_is_a_false_positive() -> None:
    # The document states a start date and no email. The backend answers with both,
    # which is the shape of the real defect this measure exists to catch: an address
    # lifted from a letterhead becomes the customer's contact address.
    corpus = make_corpus(("doc-a", {FieldName.START_DATE: "2025-01-01"}))
    answers = Answers(
        {
            "doc-a": [
                candidate(FieldName.START_DATE, "2025-01-01"),
                candidate(FieldName.EMAIL, "letterhead@example.test"),
            ]
        }
    )

    report = score_corpus(corpus, answers)

    assert (report.matched, report.wrong, report.missed) == (1, 1, 0)
    assert report.precision == 0.5
    assert report.recall == 1.0
    # Nothing was stated that went unfound, so no field is "unreachable" — the two
    # halves of the report answer different questions.
    assert report.unreachable == ()


def test_a_field_the_backend_never_produces_scores_zero_and_is_named() -> None:
    corpus = make_corpus(("doc-a", {FieldName.DOB: "1979-11-02"}))

    report = score_corpus(corpus, Answers({}))

    assert (report.matched, report.wrong, report.missed) == (0, 0, 1)
    assert report.recall == 0.0
    # Precision is defined as 0.0 rather than "no predictions, so fine": a field the
    # corpus states and the extractor never reaches is not neutral.
    assert report.precision == 0.0
    assert report.f1 == 0.0
    assert report.unreachable == (FieldName.DOB,)


def test_the_headline_is_pooled_over_fields_and_not_averaged_over_documents() -> None:
    corpus = make_corpus(
        (
            "doc-one",
            {
                FieldName.EMAIL: "a@example.test",
                FieldName.PHONE: "0123456789",
                FieldName.START_DATE: "2025-01-01",
                FieldName.END_DATE: "2026-01-01",
            },
        ),
        ("doc-two", {FieldName.DOB: "1979-11-02"}),
    )
    answers = Answers(
        {
            "doc-one": [
                candidate(FieldName.EMAIL, "a@example.test"),
                candidate(FieldName.PHONE, "0123456789"),
                candidate(FieldName.START_DATE, "2025-01-01"),
                candidate(FieldName.END_DATE, "2026-01-01"),
            ],
            "doc-two": [],
        }
    )

    report = score_corpus(corpus, answers)

    # 4 of 5 expected values found, all of them right.
    assert report.f1 == pytest.approx(8 / 9)
    # A mean of per-field F1s would be 0.8 (four perfect fields and a zero at equal
    # weight). The pooled number is what "how good is this extractor" means.
    assert report.f1 > 0.8


def test_a_value_no_normalizer_accepts_is_discarded_rather_than_counted() -> None:
    corpus = make_corpus(("doc-a", {FieldName.EMAIL: "a@example.test"}))
    answers = Answers({"doc-a": [candidate(FieldName.EMAIL, "not an address")]})

    report = score_corpus(corpus, answers)

    # The write path would drop this candidate, so it is neither a hit nor a wrong
    # answer. Counted separately, because otherwise a document of junk and an empty
    # document read identically.
    assert report.discarded == 1
    assert (report.matched, report.wrong, report.missed) == (0, 0, 1)


def test_the_value_scored_is_the_one_the_write_path_would_store() -> None:
    corpus = make_corpus(("doc-a", {FieldName.EMAIL: "right@example.test"}))
    answers = Answers(
        {
            "doc-a": [
                candidate(FieldName.EMAIL, "wrong@example.test", confidence=0.5),
                candidate(FieldName.EMAIL, "right@example.test", confidence=0.95),
            ]
        }
    )

    report = score_corpus(corpus, answers)

    # Proves the evaluator reuses best_per_field rather than scoring every candidate:
    # a second copy of "which value wins" is how a measurement stops describing the
    # thing it claims to measure.
    assert report.matched == 1
    assert report.wrong == 0


def test_the_summary_carries_counts_and_field_names_but_no_values(
    measured: EvaluationReport,
) -> None:
    summary = measured.as_dict()

    assert summary["documents"] == 8
    assert {"f1", "precision", "recall", "matched", "wrong", "missed"} <= set(summary)
    # The values in the corpus are PII-shaped. The record an operator logs must be
    # countable without any of them being quotable.
    assert "@" not in json.dumps(summary)


# --------------------------------------------------------------------------- #
# The corpus refuses to be wrong
# --------------------------------------------------------------------------- #


def test_expectations_are_loaded_in_the_form_the_database_stores(tmp_path: Path) -> None:
    manifest = write_corpus(
        tmp_path,
        [
            entry(
                {
                    "email": "Priya.Raman@Example.test",
                    "contract_value": "1,248.00",
                    "phone": "+44 20 7946 0132",
                }
            )
        ],
    )

    loaded = load_golden_corpus(manifest)

    assert loaded.documents[0].expected == {
        FieldName.EMAIL: "priya.raman@example.test",
        FieldName.CONTRACT_VALUE: "1248",
        FieldName.PHONE: "+442079460132",
    }


def test_a_field_the_pipeline_does_not_extract_is_refused(tmp_path: Path) -> None:
    manifest = write_corpus(tmp_path, [entry({"phone_number": "0123456789"})])

    with pytest.raises(EvaluationRefusedError, match="phone_number"):
        load_golden_corpus(manifest)


def test_two_documents_must_have_different_names(tmp_path: Path) -> None:
    manifest = write_corpus(
        tmp_path,
        [entry({"email": "a@example.test"}, name="one"), entry({"email": "b@example.test"})],
    )

    with pytest.raises(EvaluationRefusedError, match="one"):
        load_golden_corpus(manifest)


def test_a_document_may_not_point_outside_the_corpus(tmp_path: Path) -> None:
    # The escape is proved to exist: a check that only refuses a name would pass
    # this test even if the file it protects were there.
    secret = tmp_path / "secret.txt"
    secret.write_text("not part of any corpus", encoding="utf-8")
    corpus_root = tmp_path / "corpus"
    corpus_root.mkdir()
    manifest = write_corpus(corpus_root, [entry({"email": "a@example.test"}, file="../secret.txt")])

    with pytest.raises(EvaluationRefusedError, match="relative path"):
        load_golden_corpus(manifest)


def test_an_absolute_document_path_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("elsewhere", encoding="utf-8")
    corpus_root = tmp_path / "corpus"
    corpus_root.mkdir()
    manifest = write_corpus(corpus_root, [entry({"email": "a@example.test"}, file=str(outside))])

    with pytest.raises(EvaluationRefusedError, match="relative path"):
        load_golden_corpus(manifest)


def test_a_missing_document_is_refused(tmp_path: Path) -> None:
    (tmp_path / "doc.txt").unlink(missing_ok=True)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"documents": [entry({"email": "a@example.test"}, file="absent.txt")]}),
        encoding="utf-8",
    )

    with pytest.raises(EvaluationRefusedError, match="missing"):
        load_golden_corpus(manifest)


def test_a_blank_document_is_refused(tmp_path: Path) -> None:
    manifest = write_corpus(tmp_path, [entry({"email": "a@example.test"})], document="   \n\n")

    with pytest.raises(EvaluationRefusedError, match="empty"):
        load_golden_corpus(manifest)


def test_a_manifest_that_is_not_json_is_refused(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{not json", encoding="utf-8")

    with pytest.raises(EvaluationRefusedError, match="JSON"):
        load_golden_corpus(manifest)


def test_a_manifest_with_no_documents_is_refused(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"documents": []}), encoding="utf-8")

    with pytest.raises(EvaluationRefusedError, match="at least one document"):
        load_golden_corpus(manifest)


def test_a_document_with_no_expectations_is_refused(tmp_path: Path) -> None:
    manifest = write_corpus(tmp_path, [entry({})])

    with pytest.raises(EvaluationRefusedError, match="at least one field"):
        load_golden_corpus(manifest)


def test_an_expectation_no_normalizer_accepts_is_refused_and_not_echoed(
    tmp_path: Path,
) -> None:
    # A phone number typed into the email slot: the corpus is wrong, and saying so
    # must not print the number back into a log.
    manifest = write_corpus(tmp_path, [entry({"email": "07700900123"})])

    with pytest.raises(EvaluationRefusedError) as refused:
        load_golden_corpus(manifest)

    message = str(refused.value)
    assert "email" in message
    assert "07700900123" not in message


def test_a_date_must_be_written_in_iso_form(tmp_path: Path) -> None:
    # The canonical form lives in normalize_value and nowhere else. A manifest that
    # writes 31/12/2026 is refused rather than guessed at, because the same string
    # is 31 December in one locale and nothing at all in another.
    manifest = write_corpus(tmp_path, [entry({"end_date": "31/12/2026"})])

    with pytest.raises(EvaluationRefusedError, match="end_date"):
        load_golden_corpus(manifest)


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #


def test_the_committed_corpus_meets_the_gate(measured: EvaluationReport) -> None:
    assert measured.documents == 8
    assert measured.meets(DEFAULT_MIN_F1), measured.as_dict()


def test_the_corpus_names_the_fields_the_pattern_backend_cannot_reach(
    measured: EvaluationReport,
) -> None:
    # Gap 17, as a pair of numbers rather than a note: the backend has no rule for
    # these at all, so no amount of corpus will move them. The LLM backend is what
    # they are for.
    assert measured.unreachable == (FieldName.ADDRESS, FieldName.DOB)
    assert score_of(measured, FieldName.ADDRESS).recall == 0.0
    assert score_of(measured, FieldName.DOB).recall == 0.0


def test_a_channel_that_stops_being_extracted_falls_below_the_gate(
    corpus: GoldenCorpus,
) -> None:
    class WithoutPhone:
        """The real backend with one channel removed — a plausible regression."""

        def __init__(self) -> None:
            self._inner = PatternExtractor()

        def extract(self, text: str) -> list[FieldCandidate]:
            return [
                found
                for found in self._inner.extract(text)
                if found.field_name is not FieldName.PHONE
            ]

    report = score_corpus(corpus, WithoutPhone())

    # A gate only ever observed green is indistinguishable from a constant.
    assert not report.meets(DEFAULT_MIN_F1)
    assert report.f1 < DEFAULT_MIN_F1
    assert score_of(report, FieldName.PHONE).missed == 5


def test_an_ambiguous_date_is_left_unfound_rather_than_guessed(
    corpus: GoldenCorpus,
) -> None:
    staffing = next(doc for doc in corpus.documents if doc.name == "staffing-agreement")

    found = {item.field_name for item in PatternExtractor().extract(staffing.text)}

    # "03/04/2026 (3 April 2026)" is legible to a person and ambiguous to a regex.
    # Dropping it is the documented rule — a wrong end_date sends a renewal reminder
    # on the wrong day, the exact failure this product exists to prevent.
    assert FieldName.END_DATE not in found
    assert FieldName.START_DATE in found


def test_the_letterhead_address_is_reported_as_a_false_positive(
    measured: EvaluationReport,
) -> None:
    # The most useful line in the report, and deliberately not hidden: the utility
    # notice's only email belongs to the water company, and the backend lifts it at
    # 0.85 — above the review threshold, so it would reach a customer.
    email = score_of(measured, FieldName.EMAIL)

    assert email.wrong == 1
    assert email.recall == 1.0


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


def test_the_command_exits_zero_on_the_committed_corpus(settings: Settings) -> None:
    class ForbiddenFactory:
        """Fails the run if the measurement ever asks for a database."""

        def __call__(self) -> object:
            raise AssertionError("evaluate-extraction must not open a session")

    code = cli_main(
        ["evaluate-extraction", "--golden", str(CORPUS)],
        settings=settings,
        session_factory=cast(sessionmaker[Session], ForbiddenFactory()),
    )

    assert code == EXIT_OK


def test_the_command_exits_below_gate_for_a_corpus_it_cannot_meet(
    tmp_path: Path,
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    # A corpus of one document stating only the field the pattern backend cannot
    # reach: the measurement is 0.0, and 0.0 is what the exit code has to say.
    manifest = write_corpus(tmp_path, [entry({"dob": "1979-11-02"})])

    code = cli_main(
        ["evaluate-extraction", "--golden", str(manifest)],
        settings=settings,
        session_factory=session_factory,
    )

    assert code == EXIT_BELOW_GATE


def test_the_command_exits_failed_for_a_manifest_it_cannot_use(
    tmp_path: Path,
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    manifest = write_corpus(tmp_path, [entry({"phone_number": "0123456789"})])

    code = cli_main(
        ["evaluate-extraction", "--golden", str(manifest)],
        settings=settings,
        session_factory=session_factory,
    )

    assert code == EXIT_FAILED


def test_the_threshold_must_be_a_fraction(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["evaluate-extraction", "--golden", str(CORPUS), "--min-f1", "85"]
        )

    # A percentage typed by habit would otherwise fail every run while looking like
    # a legitimate gate, and the operator would read it as extraction getting worse.
    assert "between 0.0 and 1.0" in capsys.readouterr().err
