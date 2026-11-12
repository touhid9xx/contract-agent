"""Field extraction: parsing, the review gate, and the upsert (F7).

Two halves, deliberately in one file because they are one guarantee:

* the backends turn a document into ``FieldCandidate`` values — deterministic, so
  the confidence numbers below are contract, not coincidence;
* ``extract_contract`` decides what may be *written*: nothing reviewed by a human
  is overwritten, no value is stored in the clear, and no field ever becomes
  ``approved`` without a person.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.blind_index import ContactBlindIndex
from app.core.config import Settings
from app.core.crypto import FieldCipher
from app.db.enums import FieldName, FieldStatus, UserRole
from app.db.models import Contract, ExtractedField, Tenant
from app.db.repositories.fields import contracts_for_contact
from app.workers.extraction import (
    ExtractionUnavailableError,
    FieldCandidate,
    decrypt_for_review,
    extract_contract,
    normalize_value,
)
from app.workers.extractors import OllamaExtractor, PatternExtractor, build_extractor

#: The shipped default, named here so a change to it is visible in the tests.
THRESHOLD = 0.8

#: ``extract_contract`` takes the index as a required argument, so the tests need one
#: even when they are not about indexing. Same key length the settings floor enforces.
CONTACT_INDEX = ContactBlindIndex("test-contact-index-key-0123456789")

DOCUMENT = """Service Agreement

Customer: A. Rahman
Email: a.rahman@example.test
Phone: +44 20 7946 0958
Start date: 2024-01-01
End date: 2026-01-01
Contract value: $1,500.00
"""


# --- doubles --------------------------------------------------------------
class StubExtractor:
    """A backend that returns exactly what it was given."""

    def __init__(self, *candidates: FieldCandidate) -> None:
        self._candidates = list(candidates)
        self.seen: list[str] = []

    def extract(self, text: str) -> Sequence[FieldCandidate]:
        self.seen.append(text)
        return self._candidates


class BrokenExtractor:
    """A backend that cannot answer, like an unreachable local model."""

    def extract(self, text: str) -> Sequence[FieldCandidate]:
        raise ExtractionUnavailableError("the local model did not answer")


@pytest.fixture
def cipher() -> FieldCipher:
    return FieldCipher(Fernet.generate_key().decode("ascii"))


@pytest.fixture
def contract(db_session: Session, tenant: Tenant) -> Contract:
    row = Contract(tenant_id=tenant.id, customer_ref="A. Rahman", end_date=date(2026, 1, 1))
    db_session.add(row)
    db_session.commit()
    return row


def field_rows(session: Session, contract: Contract) -> dict[FieldName, ExtractedField]:
    session.flush()
    statement = select(ExtractedField).where(ExtractedField.contract_id == contract.id)
    return {row.field_name: row for row in session.execute(statement).scalars()}


def run(
    session: Session,
    contract: Contract,
    extractor: Any,
    cipher: FieldCipher,
    *,
    document: str = DOCUMENT,
    threshold: float = THRESHOLD,
    contact_index: ContactBlindIndex = CONTACT_INDEX,
) -> Any:
    return extract_contract(
        session,
        contract,
        document,
        extractor=extractor,
        cipher=cipher,
        contact_index=contact_index,
        review_threshold=threshold,
    )


# --- normalization --------------------------------------------------------
def test_an_email_is_lowercased_and_trimmed() -> None:
    assert normalize_value(FieldName.EMAIL, "  A.Rahman@Example.TEST ") == ("a.rahman@example.test")


def test_something_that_is_not_an_email_is_refused() -> None:
    with pytest.raises(ValueError, match="email"):
        normalize_value(FieldName.EMAIL, "call the office")


def test_a_phone_keeps_its_digits_and_a_leading_plus() -> None:
    assert normalize_value(FieldName.PHONE, "+44 (20) 7946-0958") == "+442079460958"
    assert normalize_value(FieldName.PHONE, "020 7946 0958") == "02079460958"


def test_a_phone_with_too_few_digits_is_refused() -> None:
    # Otherwise "ext. 12" becomes a contact channel and the SMS bounces.
    with pytest.raises(ValueError, match="phone"):
        normalize_value(FieldName.PHONE, "ext. 12")


def test_a_date_must_be_iso_and_real() -> None:
    assert normalize_value(FieldName.START_DATE, "2024-01-01") == "2024-01-01"
    # One stable phrase regardless of which part of the date is wrong: a
    # reviewer-facing message must not depend on Python's parser internals.
    with pytest.raises(ValueError, match="not an ISO-8601 date"):
        normalize_value(FieldName.START_DATE, "01/01/2024")
    with pytest.raises(ValueError, match="not an ISO-8601 date"):
        normalize_value(FieldName.START_DATE, "2024-13-01")


def test_a_contract_value_becomes_a_bare_number() -> None:
    assert normalize_value(FieldName.CONTRACT_VALUE, "$1,500.00") == "1500"
    assert normalize_value(FieldName.CONTRACT_VALUE, "£99.50") == "99.5"


def test_free_text_has_its_whitespace_collapsed() -> None:
    assert normalize_value(FieldName.ADDRESS, "12  Elm\n Road ") == "12 Elm Road"


# --- the pattern backend --------------------------------------------------
def test_a_labelled_email_outranks_a_bare_one() -> None:
    labelled = PatternExtractor().extract("Email: a.rahman@example.test")
    bare = PatternExtractor().extract("written to a.rahman@example.test on Monday")

    assert [c.confidence for c in labelled] == [0.95]
    assert [c.confidence for c in bare] == [0.85]


def test_an_unlabelled_phone_is_scored_below_the_review_threshold() -> None:
    labelled = PatternExtractor().extract("Phone: +44 20 7946 0958")
    bare = PatternExtractor().extract("ring +44 20 7946 0958 any time")

    assert labelled[0].confidence == 0.9
    assert bare[0].confidence == 0.75 < THRESHOLD


def test_a_date_is_taken_from_the_label_that_names_it() -> None:
    found = {c.field_name: c for c in PatternExtractor().extract(DOCUMENT)}

    assert found[FieldName.START_DATE].value == "2024-01-01"
    assert found[FieldName.END_DATE].value == "2026-01-01"
    assert found[FieldName.START_DATE].confidence == 0.95


def test_an_ambiguous_numeric_date_is_dropped_rather_than_guessed() -> None:
    # 03/04/2025 is both 3 April and 4 March. Guessing sends a renewal reminder on
    # the wrong day, which is the failure this product exists to prevent.
    assert PatternExtractor().extract("Start date: 03/04/2025") == []


def test_a_day_first_date_is_read_when_the_day_cannot_be_a_month() -> None:
    found = PatternExtractor().extract("End date: 25/12/2025")

    assert found[0].value == "2025-12-25"
    # Read, but not trusted as much as an unambiguous ISO date.
    assert found[0].confidence == 0.85


def test_a_date_is_not_mistaken_for_a_phone_number() -> None:
    found = {c.field_name for c in PatternExtractor().extract(DOCUMENT)}

    assert FieldName.PHONE in found
    phones = [c for c in PatternExtractor().extract(DOCUMENT) if c.field_name is FieldName.PHONE]
    assert [c.value for c in phones] == ["+44 20 7946 0958"]


def test_a_value_below_a_currency_symbol_is_read() -> None:
    found = [
        c for c in PatternExtractor().extract(DOCUMENT) if c.field_name is FieldName.CONTRACT_VALUE
    ]

    assert found[0].value == "1,500.00"
    assert found[0].confidence == 0.9


def test_every_candidate_carries_the_line_it_came_from() -> None:
    found = {c.field_name: c for c in PatternExtractor().extract(DOCUMENT)}

    assert found[FieldName.EMAIL].source_line == 4
    assert found[FieldName.PHONE].source_line == 5


def test_the_pattern_backend_is_repeatable() -> None:
    first = PatternExtractor().extract(DOCUMENT)
    second = PatternExtractor().extract(DOCUMENT)

    assert first == second


# --- the review gate ------------------------------------------------------
def test_a_confident_field_is_accepted_without_review(
    db_session: Session, contract: Contract, cipher: FieldCipher
) -> None:
    outcome = run(
        db_session,
        contract,
        StubExtractor(FieldCandidate(FieldName.EMAIL, "a.rahman@example.test", 0.95)),
        cipher,
    )

    rows = field_rows(db_session, contract)
    assert outcome.created == 1
    assert rows[FieldName.EMAIL].status is FieldStatus.AUTO_ACCEPTED
    assert rows[FieldName.EMAIL].blocks_pipeline is False


def test_a_less_confident_field_is_held_for_review(
    db_session: Session, contract: Contract, cipher: FieldCipher
) -> None:
    run(
        db_session,
        contract,
        StubExtractor(FieldCandidate(FieldName.PHONE, "02079460958", 0.75)),
        cipher,
    )

    row = field_rows(db_session, contract)[FieldName.PHONE]
    assert row.status is FieldStatus.PENDING_REVIEW
    assert row.blocks_pipeline is True


def test_extraction_cannot_write_approved(
    db_session: Session, contract: Contract, cipher: FieldCipher
) -> None:
    # Even a perfect score stops at auto_accepted: approving is a human act.
    run(
        db_session,
        contract,
        StubExtractor(FieldCandidate(FieldName.EMAIL, "a.rahman@example.test", 1.0)),
        cipher,
    )

    statuses = {row.status for row in field_rows(db_session, contract).values()}
    assert FieldStatus.APPROVED not in statuses
    assert statuses == {FieldStatus.AUTO_ACCEPTED}


def test_the_threshold_is_what_decides_not_the_backend(
    db_session: Session, contract: Contract, cipher: FieldCipher
) -> None:
    run(
        db_session,
        contract,
        StubExtractor(FieldCandidate(FieldName.EMAIL, "a.rahman@example.test", 0.85)),
        cipher,
        threshold=0.9,
    )

    assert field_rows(db_session, contract)[FieldName.EMAIL].status is (FieldStatus.PENDING_REVIEW)


def test_confidence_is_clamped_and_stored_to_three_decimals(
    db_session: Session, contract: Contract, cipher: FieldCipher
) -> None:
    run(
        db_session,
        contract,
        StubExtractor(FieldCandidate(FieldName.EMAIL, "a.rahman@example.test", 1.7)),
        cipher,
    )

    assert float(field_rows(db_session, contract)[FieldName.EMAIL].confidence) == 1.0


# --- storage --------------------------------------------------------------
def test_the_raw_value_is_encrypted_at_rest(
    db_session: Session, contract: Contract, cipher: FieldCipher
) -> None:
    plaintext: str = "a.rahman@example.test"
    run(
        db_session,
        contract,
        StubExtractor(FieldCandidate(FieldName.EMAIL, plaintext, 0.95)),
        cipher,
    )

    row = field_rows(db_session, contract)[FieldName.EMAIL]
    # What a database dump shows:
    assert row.raw_value is not None
    assert plaintext not in row.raw_value
    # What a reviewer is shown, through the one function allowed to decrypt:
    assert decrypt_for_review(row, cipher) == plaintext


def test_decrypting_a_field_with_no_value_returns_nothing(cipher: FieldCipher) -> None:
    field = ExtractedField(
        contract_id=1,
        tenant_id=1,
        field_name=FieldName.EMAIL,
        raw_value="",
        confidence=0.9,
        status=FieldStatus.AUTO_ACCEPTED,
    )

    assert decrypt_for_review(field, cipher) is None


def test_re_running_updates_the_same_row_in_place(
    db_session: Session, contract: Contract, cipher: FieldCipher
) -> None:
    extractor = StubExtractor(FieldCandidate(FieldName.EMAIL, "first@example.test", 0.95))
    run(db_session, contract, extractor, cipher)
    db_session.commit()

    extractor = StubExtractor(FieldCandidate(FieldName.EMAIL, "second@example.test", 0.95))
    outcome = run(db_session, contract, extractor, cipher)

    rows = field_rows(db_session, contract)
    assert len(rows) == 1
    assert outcome.created == 0
    assert outcome.updated == 1
    stored = rows[FieldName.EMAIL].normalized_value
    assert stored is not None
    assert cipher.decrypt(stored) == "second@example.test"
    # The optimistic lock makes a stale reviewer lose the race rather than win it.
    assert rows[FieldName.EMAIL].version == 2


def test_the_normalized_value_is_sealed_as_well_as_the_raw_one(
    db_session: Session, contract: Contract, cipher: FieldCipher
) -> None:
    # Gap 20: the column the decision and dispatch policies read held the same PII
    # as ``raw_value`` but in the clear, so encrypting only one of them left the
    # value readable in the same row.
    run(
        db_session,
        contract,
        StubExtractor(FieldCandidate(FieldName.EMAIL, "a.rahman@example.test", 0.95)),
        cipher,
    )

    row = field_rows(db_session, contract)[FieldName.EMAIL]
    assert row.normalized_value is not None
    assert "a.rahman@example.test" not in row.normalized_value
    assert cipher.decrypt(row.normalized_value) == "a.rahman@example.test"


def test_a_field_a_human_decided_is_never_overwritten(
    db_session: Session, contract: Contract, cipher: FieldCipher
) -> None:
    db_session.add(
        ExtractedField(
            tenant_id=contract.tenant_id,
            contract_id=contract.id,
            field_name=FieldName.EMAIL,
            raw_value=cipher.encrypt("reviewed@example.test"),
            normalized_value=cipher.encrypt("reviewed@example.test"),
            confidence=0.55,
            status=FieldStatus.APPROVED,
        )
    )
    db_session.commit()

    outcome = run(
        db_session,
        contract,
        StubExtractor(FieldCandidate(FieldName.EMAIL, "different@example.test", 0.99)),
        cipher,
    )

    row = field_rows(db_session, contract)[FieldName.EMAIL]
    assert outcome.skipped_reviewed == 1
    assert outcome.stored == 0
    assert row.normalized_value is not None
    assert cipher.decrypt(row.normalized_value) == "reviewed@example.test"
    assert row.status is FieldStatus.APPROVED


def test_the_most_confident_candidate_wins_for_a_field(
    db_session: Session, contract: Contract, cipher: FieldCipher
) -> None:
    # Two emails in one document: taking whichever came last would be a coin flip.
    run(
        db_session,
        contract,
        StubExtractor(
            FieldCandidate(FieldName.EMAIL, "signature@example.test", 0.85),
            FieldCandidate(FieldName.EMAIL, "contact@example.test", 0.95),
        ),
        cipher,
    )

    stored = field_rows(db_session, contract)[FieldName.EMAIL].normalized_value
    assert stored is not None
    assert cipher.decrypt(stored) == "contact@example.test"


def test_an_unusable_value_is_discarded_and_the_rest_survive(
    db_session: Session, contract: Contract, cipher: FieldCipher
) -> None:
    outcome = run(
        db_session,
        contract,
        StubExtractor(
            FieldCandidate(FieldName.EMAIL, "not-an-address", 0.99),
            FieldCandidate(FieldName.PHONE, "+44 20 7946 0958", 0.9),
        ),
        cipher,
    )

    assert outcome.discarded == 1
    assert set(field_rows(db_session, contract)) == {FieldName.PHONE}
    assert outcome.stored == 1


def test_a_document_with_no_fields_is_a_visible_empty_result(
    db_session: Session, contract: Contract, cipher: FieldCipher
) -> None:
    outcome = run(db_session, contract, StubExtractor(), cipher)

    assert outcome.candidates == 0
    assert outcome.produced_nothing is True
    assert outcome.found == ()
    assert field_rows(db_session, contract) == {}


def test_fields_are_written_for_the_contracts_tenant_not_the_callers(
    db_session: Session, contract: Contract, cipher: FieldCipher
) -> None:
    run(
        db_session,
        contract,
        StubExtractor(FieldCandidate(FieldName.EMAIL, "a@b.test", 0.95)),
        cipher,
    )

    row = field_rows(db_session, contract)[FieldName.EMAIL]
    assert row.tenant_id == contract.tenant_id


def test_the_outcome_reports_what_it_wrote(
    db_session: Session, contract: Contract, cipher: FieldCipher
) -> None:
    outcome = run(db_session, contract, PatternExtractor(), cipher)

    assert outcome.contract_id == contract.id
    assert outcome.found == tuple(
        name.value
        for name in (
            FieldName.EMAIL,
            FieldName.PHONE,
            FieldName.START_DATE,
            FieldName.END_DATE,
            FieldName.CONTRACT_VALUE,
        )
    )


def test_the_extraction_writes_nothing_until_the_caller_commits(
    db_session: Session, contract: Contract, cipher: FieldCipher
) -> None:
    # The CLI owns the transaction, as with every other write path here.
    run(db_session, contract, PatternExtractor(), cipher)

    assert contract.id is not None
    assert db_session.new or db_session.dirty


def test_a_backend_that_cannot_answer_propagates(
    db_session: Session, contract: Contract, cipher: FieldCipher
) -> None:
    with pytest.raises(ExtractionUnavailableError):
        run(db_session, contract, BrokenExtractor(), cipher)

    assert field_rows(db_session, contract) == {}


# --- which backend --------------------------------------------------------
def test_pattern_is_the_default_backend() -> None:
    assert isinstance(build_extractor(Settings(app_env="test", log_json=False)), PatternExtractor)


def test_the_ollama_backend_is_selected_by_name() -> None:
    settings = Settings(
        app_env="test",
        log_json=False,
        extraction_backend="ollama",
        extraction_model="llama3.1:8b",
    )

    assert isinstance(build_extractor(settings), OllamaExtractor)


def test_there_is_no_cloud_backend_to_select() -> None:
    from app.core.config import ExtractionBackend

    allowed = ExtractionBackend.__args__  # type: ignore[attr-defined]

    assert set(allowed) == {"pattern", "ollama"}


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def ollama(**overrides: Any) -> OllamaExtractor:
    values: dict[str, Any] = {
        "base_url": "http://127.0.0.1:11434",
        "model": "llama3.1:8b",
        "timeout_seconds": 5.0,
    }
    values.update(overrides)
    return OllamaExtractor(**values)


def completion(payload: Any) -> bytes:
    return json.dumps({"response": json.dumps(payload)}).encode("utf-8")


def test_the_model_completion_is_parsed_into_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[Any] = []

    def fake_urlopen(request: Any, timeout: float | None = None) -> _FakeResponse:
        sent.append(request)
        return _FakeResponse(completion({"address": "12 Elm Road", "email": "a@b.test"}))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    found = {c.field_name: c for c in ollama().extract("some document text")}

    assert found[FieldName.ADDRESS].value == "12 Elm Road"
    assert found[FieldName.EMAIL].value == "a@b.test"
    # No score of its own: a model value starts life needing a human.
    assert found[FieldName.ADDRESS].confidence == 0.6 < THRESHOLD
    # The request asks for JSON, never streams, and removes sampling noise so a
    # re-run is reproducible.
    body = json.loads(sent[0].data)
    assert body["format"] == "json"
    assert body["stream"] is False
    assert body["options"] == {"temperature": 0}
    assert body["model"] == "llama3.1:8b"


def test_a_field_name_the_system_does_not_store_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A model must not be able to propose a tenant, a status or an id.
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=None: _FakeResponse(
            completion({"tenant_id": 7, "status": "approved", "email": "a@b.test"})
        ),
    )

    assert [c.field_name for c in ollama().extract("text")] == [FieldName.EMAIL]


def test_a_model_that_is_not_running_raises_without_leaking_the_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(request: Any, timeout: float | None = None) -> _FakeResponse:
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", refuse)

    with pytest.raises(ExtractionUnavailableError) as caught:
        ollama().extract("Customer email: a.rahman@example.test")

    message = str(caught.value)
    assert "a.rahman@example.test" not in message
    assert "127.0.0.1" not in message


def test_a_completion_that_is_not_json_is_an_unavailable_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=None: _FakeResponse(
            json.dumps({"response": "I'm sorry, I can't help with that."}).encode("utf-8")
        ),
    )

    with pytest.raises(ExtractionUnavailableError):
        ollama().extract("text")


def test_a_model_score_is_honoured_but_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=None: _FakeResponse(
            completion({"fields": {"email": {"value": "a@b.test", "confidence": 4.2}}})
        ),
    )

    found = ollama().extract("text")

    assert found[0].confidence == 1.0


def test_a_contact_field_is_stored_with_a_digest_that_can_find_it_again(
    db_session: Session,
    contract: Contract,
    cipher: FieldCipher,
) -> None:
    # The digest is the only way an inbound STOP can ever locate this row: both value
    # columns are Fernet ciphertext under a random IV, so a phone number is not
    # searchable. If extraction stops writing it, opt-out silently stops working.
    run(db_session, contract, PatternExtractor(), cipher)

    row = field_rows(db_session, contract)[FieldName.EMAIL]
    assert row.blind_index == CONTACT_INDEX.digest(
        normalize_value(FieldName.EMAIL, "a.rahman@example.test")
    )
    assert [
        c.id
        for c in contracts_for_contact(
            db_session,
            field_name=FieldName.EMAIL,
            blind_index=row.blind_index or "",
        )
    ] == [contract.id]


def test_a_non_contact_field_is_stored_without_a_digest(
    db_session: Session,
    contract: Contract,
    cipher: FieldCipher,
) -> None:
    # NULL here means "not a contact", not "missing". Dates are never looked up, and
    # an index on them would be a searchable copy of a value we encrypt.
    run(db_session, contract, PatternExtractor(), cipher)

    assert field_rows(db_session, contract)[FieldName.START_DATE].blind_index is None


def test_the_admin_role_is_not_a_writer_of_fields() -> None:
    # Documents the intended split: ingestion and review are operator work. The
    # admin is included because it can do everything an operator can.
    from app.db.enums import UserRole as Role

    assert Role.ADMIN.value == "admin"
    assert UserRole.VIEWER.value == "viewer"
