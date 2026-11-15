"""The extraction worker loop: what it claims, what it stamps, and what it refuses to.

The worker's contract is four promises, and every test below pins one of them:

* it claims ``storage_path IS NOT NULL AND extraction_attempted_at IS NULL``, and
  nothing else — no status filter, no tenant filter;
* it stamps ``extraction_attempted_at`` only when the document was actually read,
  so ``unreadable`` and ``unavailable`` are retried rather than silently dropped;
* its cursor carries past a contract it cannot finish, so one broken document
  costs one tick and not the whole queue;
* it counts and gauges only after the page commits, because a series that names
  work a rolled-back page still holds makes the dashboard lie.

The distinction between "the document cannot be read" and "the backend is down"
is tested in both directions on purpose: they produce different outcomes, and
both stay unstamped, which is what makes either repair effective.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core import metrics
from app.core.blind_index import ContactBlindIndex, build_blind_index
from app.core.config import Settings
from app.core.crypto import FieldCipher, build_cipher
from app.core.documents import build_document_store
from app.db.enums import ContractStatus, FieldName, FieldStatus
from app.db.models import Contract, ExtractedField, Tenant
from app.db.session import session_scope
from app.workers.extraction import ExtractionUnavailableError, FieldCandidate
from app.workers.extraction_worker import ExtractionResult, ExtractionWorker

#: Fixed so ``extraction_attempted_at`` can be compared exactly rather than "recently".
#:
#: Naive UTC on purpose, like ``NOW`` in ``test_expiry`` and ``test_reminder_planner``.
#: ``app.db.base`` records the rule: ``DateTime(timezone=False)`` discards ``tzinfo``, so
#: every stored timestamp is naive UTC and ``utc_now()`` returns one. An aware constant
#: here would make the worker write a value the column cannot hold and would compare
#: unequal to what reads back.
MOMENT = datetime(2026, 3, 1, 9, 0)
LATER = datetime(2026, 3, 1, 9, 15)

#: A document the pattern backend reads confidently: a labelled email is scored 0.95,
#: above the 0.8 review threshold, so the row lands ``auto_accepted`` and counts as stored.
DOCUMENT = b"""Service Agreement

Customer: A. Rahman
Email: a.rahman@example.test
Start date: 2024-01-01
End date: 2026-01-01
"""

EXTRACTED_EMAIL = "a.rahman@example.test"

#: A document whose *only* extractable field is the email. Used where the test needs a run
#: that finishes with nothing written: with ``DOCUMENT`` the start and end dates would be
#: stored, and ``stored > 0`` outranks ``skipped_reviewed > 0`` by design (see
#: ``ExtractionWorker._classify``), so the page would correctly report ``extracted``.
EMAIL_ONLY_DOCUMENT = b"""Service Agreement

Customer: A. Rahman
Email: a.rahman@example.test
"""

#: The reviewer's value, deliberately different from the document's, so an overwrite
#: would be visible as a different decrypted value rather than a changed status.
REVIEWED_EMAIL = "reviewed@example.test"


# --- doubles --------------------------------------------------------------
class BrokenExtractor:
    """A backend that cannot answer, like an unreachable local model."""

    def extract(self, text: str) -> Sequence[FieldCandidate]:
        raise ExtractionUnavailableError("the local model did not answer")


class DoomedExtractor:
    """A backend that fails in a way the worker does *not* treat as an outcome.

    ``ExtractionUnavailableError`` is a shared, transient condition the page absorbs.
    Anything else is a bug, and it must reach the caller so the transaction rolls back.
    """

    def extract(self, text: str) -> Sequence[FieldCandidate]:
        raise RuntimeError("the extractor is broken in a way nobody planned for")


# --- fixtures -------------------------------------------------------------
@pytest.fixture(autouse=True)
def clean_registry():
    """Every test starts and ends with empty samples.

    The worker counts after its commit, so the counters are part of the behaviour
    worth asserting — and an absolute assertion needs a known starting point.
    """
    metrics.reset()
    yield
    metrics.reset()


@pytest.fixture
def worker_settings(settings: Settings, tmp_path: Path) -> Settings:
    """Settings that can seal, read and index a document, in the test's own directory.

    ``CONTACT_BLIND_INDEX_KEY`` is not optional here: the worker builds its cipher and
    its index eagerly in ``__init__``, so a missing key fails at start-up rather than
    at the first contact value — which is the behaviour a deployment wants.
    """
    return settings.model_copy(
        update={
            "encryption_key": Fernet.generate_key().decode("ascii"),
            "contact_blind_index_key": "extraction-worker-test-index-key-01",
            "document_store_path": str(tmp_path / "documents"),
        }
    )


@pytest.fixture
def cipher(worker_settings: Settings) -> FieldCipher:
    return build_cipher(worker_settings)


@pytest.fixture
def contact_index(worker_settings: Settings) -> ContactBlindIndex:
    return build_blind_index(worker_settings)


# --- helpers --------------------------------------------------------------
def store_document(settings: Settings, cipher: FieldCipher, payload: bytes) -> str:
    """Seal ``payload`` into the configured store and return its relative key."""
    return build_document_store(settings, cipher=cipher).save(payload).key


def make_contract(
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    *,
    storage_path: str | None,
) -> int:
    """A contract row with the given ``storage_path`` and no extraction stamp yet."""
    with session_scope(session_factory) as session:
        row = Contract(
            tenant_id=tenant.id,
            customer_ref="A. Rahman",
            timezone="UTC",
            status=ContractStatus.ACTIVE,
            storage_path=storage_path,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def make_stored_contract(
    session_factory: sessionmaker[Session],
    settings: Settings,
    cipher: FieldCipher,
    tenant: Tenant,
    *,
    payload: bytes = DOCUMENT,
) -> int:
    """A contract whose ``storage_path`` names a sealed copy of ``payload``."""
    return make_contract(
        session_factory,
        tenant,
        storage_path=store_document(settings, cipher, payload),
    )


def add_reviewed_email(
    session_factory: sessionmaker[Session],
    cipher: FieldCipher,
    tenant: Tenant,
    contract_id: int,
    *,
    value: str = REVIEWED_EMAIL,
) -> None:
    """A field a human already approved, which extraction must not overwrite."""
    with session_scope(session_factory) as session:
        session.add(
            ExtractedField(
                tenant_id=tenant.id,
                contract_id=contract_id,
                field_name=FieldName.EMAIL,
                raw_value=cipher.encrypt(value),
                normalized_value=cipher.encrypt(value),
                confidence=0.55,
                status=FieldStatus.APPROVED,
            )
        )


def worker_for(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    batch_size: int = 10,
    extractor: Any | None = None,
) -> ExtractionWorker:
    """A worker whose extractor is either the real one built from settings, or a double."""
    return ExtractionWorker(
        session_factory,
        settings=settings,
        batch_size=batch_size,
        poll_interval_seconds=0.01,
        extractor=extractor,
    )


def stamp_of(session_factory: sessionmaker[Session], contract_id: int) -> datetime | None:
    """``contracts.extraction_attempted_at`` as committed, or ``None`` from a fresh read."""
    with session_factory() as session:
        row = session.get(Contract, contract_id)
        assert row is not None, "the contract vanished from under the test"
        return row.extraction_attempted_at


def fields_of(
    session_factory: sessionmaker[Session], contract_id: int
) -> dict[FieldName, ExtractedField]:
    """Every extracted field of one contract, keyed by name, read after the fact."""
    with session_factory() as session:
        statement = select(ExtractedField).where(ExtractedField.contract_id == contract_id)
        return {row.field_name: row for row in session.execute(statement).scalars()}


# --- the happy path -------------------------------------------------------
def test_a_stored_document_is_extracted_and_stamped(
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
    cipher: FieldCipher,
    tenant: Tenant,
) -> None:
    contract_id = make_stored_contract(session_factory, worker_settings, cipher, tenant)

    result = worker_for(session_factory, worker_settings).run_once(now=MOMENT)

    assert result.candidates == 1
    assert result.extracted == 1
    assert result.stamped == 1
    assert result.worked is True
    assert stamp_of(session_factory, contract_id) == MOMENT
    assert FieldName.EMAIL in fields_of(session_factory, contract_id)


def test_the_value_that_was_stored_is_the_one_in_the_document(
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
    cipher: FieldCipher,
    tenant: Tenant,
) -> None:
    contract_id = make_stored_contract(session_factory, worker_settings, cipher, tenant)

    worker_for(session_factory, worker_settings).run_once(now=MOMENT)

    row = fields_of(session_factory, contract_id)[FieldName.EMAIL]
    assert row.normalized_value is not None
    assert cipher.decrypt(row.normalized_value) == EXTRACTED_EMAIL


def test_a_human_is_never_the_thing_that_approves_a_field(
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
    cipher: FieldCipher,
    tenant: Tenant,
) -> None:
    """The stricter form of the guarantee ``extract_contract`` owns, asserted once here.

    A confidently read email lands ``auto_accepted`` — the pipeline may use it — but
    never ``approved``, which is a verdict only ``/review`` can write.
    """
    contract_id = make_stored_contract(session_factory, worker_settings, cipher, tenant)

    worker_for(session_factory, worker_settings).run_once(now=MOMENT)

    status = fields_of(session_factory, contract_id)[FieldName.EMAIL].status
    assert status is FieldStatus.AUTO_ACCEPTED


# --- idempotence and the claim predicate ----------------------------------
def test_a_second_tick_finds_nothing_because_the_first_stamped_it(
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
    cipher: FieldCipher,
    tenant: Tenant,
) -> None:
    contract_id = make_stored_contract(session_factory, worker_settings, cipher, tenant)
    worker = worker_for(session_factory, worker_settings)

    worker.run_once(now=MOMENT)
    second = worker.run_once(now=LATER)

    assert second.candidates == 0
    assert second.stamped == 0
    assert second.worked is False
    # The stamp is the evidence of the *first* run; a second tick must not refresh it.
    assert stamp_of(session_factory, contract_id) == MOMENT


def test_a_tick_with_an_empty_queue_is_a_no_op(
    session_factory: sessionmaker[Session], worker_settings: Settings
) -> None:
    result = worker_for(session_factory, worker_settings).run_once(now=MOMENT)

    assert result == ExtractionResult()


def test_a_contract_ingested_as_metadata_only_is_never_claimed(
    session_factory: sessionmaker[Session], worker_settings: Settings, tenant: Tenant
) -> None:
    """No document means nothing to extract, so the row is not work.

    It is also never stamped: the queue is defined by ``storage_path IS NOT NULL``,
    so a metadata-only contract stays out of the backlog for as long as it has no file.
    """
    contract_id = make_contract(session_factory, tenant, storage_path=None)

    result = worker_for(session_factory, worker_settings).run_once(now=MOMENT)

    assert result.candidates == 0
    assert stamp_of(session_factory, contract_id) is None


def test_the_status_of_a_contract_does_not_decide_whether_it_is_extracted(
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
    cipher: FieldCipher,
    tenant: Tenant,
) -> None:
    """Extraction is about the document, not the lifecycle stage.

    A contract already marked ``expired`` still has a document worth reading — and the
    claim predicate deliberately has no status filter, which this pins.
    """
    contract_id = make_stored_contract(session_factory, worker_settings, cipher, tenant)
    with session_scope(session_factory) as session:
        row = session.get(Contract, contract_id)
        assert row is not None
        row.status = ContractStatus.EXPIRED

    result = worker_for(session_factory, worker_settings).run_once(now=MOMENT)

    assert result.extracted == 1


# --- the three finishing outcomes -----------------------------------------
def test_a_document_that_yields_nothing_is_stamped_as_empty(
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
    cipher: FieldCipher,
    tenant: Tenant,
) -> None:
    """``empty`` is a finished run, not a failure: the document was read and had nothing.

    Stamping it is the whole point of the outcome — an empty document retried every
    fifteen minutes forever would be a tick that never converges.
    """
    contract_id = make_stored_contract(
        session_factory,
        worker_settings,
        cipher,
        tenant,
        payload=b"Facilities maintenance. Nothing to extract from this line.\n",
    )

    result = worker_for(session_factory, worker_settings).run_once(now=MOMENT)

    assert result.empty == 1
    assert result.extracted == 0
    assert stamp_of(session_factory, contract_id) == MOMENT
    assert fields_of(session_factory, contract_id) == {}


def test_a_contract_whose_document_is_already_reviewed_is_stamped_as_reviewed(
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
    cipher: FieldCipher,
    tenant: Tenant,
) -> None:
    """Every field the document offers is owned by a human, so nothing was written."""
    contract_id = make_stored_contract(
        session_factory,
        worker_settings,
        cipher,
        tenant,
        payload=EMAIL_ONLY_DOCUMENT,
    )
    add_reviewed_email(session_factory, cipher, tenant, contract_id)

    result = worker_for(session_factory, worker_settings).run_once(now=MOMENT)

    assert result.reviewed == 1
    assert result.extracted == 0
    assert stamp_of(session_factory, contract_id) == MOMENT
    row = fields_of(session_factory, contract_id)[FieldName.EMAIL]
    assert row.status is FieldStatus.APPROVED
    assert row.normalized_value is not None
    assert cipher.decrypt(row.normalized_value) == REVIEWED_EMAIL


def test_a_partly_reviewed_document_still_counts_as_extracted(
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
    cipher: FieldCipher,
    tenant: Tenant,
) -> None:
    """``stored`` outranks ``skipped_reviewed``, and that precedence is deliberate.

    ``DOCUMENT`` yields a start and an end date as well as the email. With the email
    already owned by a reviewer, extraction writes the two dates and leaves the human's
    value alone — so the run stored something, and "the reviewer owns every field" is
    not what happened. Reporting ``reviewed`` here would describe a page that changed
    nothing, when the interesting fact is that two fields appeared.
    """
    contract_id = make_stored_contract(session_factory, worker_settings, cipher, tenant)
    add_reviewed_email(session_factory, cipher, tenant, contract_id)

    result = worker_for(session_factory, worker_settings).run_once(now=MOMENT)

    assert result.extracted == 1
    assert result.reviewed == 0
    stored = fields_of(session_factory, contract_id)
    assert FieldName.END_DATE in stored, "the fields the reviewer did not own were stored"
    approved = stored[FieldName.EMAIL]
    assert approved.status is FieldStatus.APPROVED
    assert approved.normalized_value is not None
    assert cipher.decrypt(approved.normalized_value) == REVIEWED_EMAIL


# --- what is deliberately left in the queue -------------------------------
def test_a_document_that_cannot_be_read_is_left_for_the_next_tick(
    session_factory: sessionmaker[Session], worker_settings: Settings, tenant: Tenant
) -> None:
    """A ``storage_path`` naming a blob the store does not hold is an operator problem.

    Unstamped on purpose: the row keeps its place, and mounting the missing volume is
    all it takes for the next tick to finish it.
    """
    contract_id = make_contract(session_factory, tenant, storage_path="not-a-real-key")

    result = worker_for(session_factory, worker_settings).run_once(now=MOMENT)

    assert result.unreadable == 1
    assert result.stamped == 0
    assert stamp_of(session_factory, contract_id) is None


def test_a_document_over_the_character_limit_is_refused_rather_than_truncated(
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
    cipher: FieldCipher,
    tenant: Tenant,
) -> None:
    """Truncation would manufacture missing fields, so the worker refuses instead.

    The run stays unstamped because the repair is a setting, and the retry after
    raising it is the point.
    """
    contract_id = make_stored_contract(session_factory, worker_settings, cipher, tenant)
    limited = worker_settings.model_copy(update={"extraction_max_document_chars": 10})

    result = worker_for(session_factory, limited).run_once(now=MOMENT)

    assert result.unreadable == 1
    assert stamp_of(session_factory, contract_id) is None


def test_a_backend_that_cannot_answer_does_not_stop_the_page(
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
    cipher: FieldCipher,
    tenant: Tenant,
) -> None:
    """``ExtractionUnavailableError`` is the one exception the worker absorbs.

    It is a condition the whole page shares, so letting it out would mean a page of ten
    contracts makes no progress because a model did not answer for one of them.
    """
    first = make_stored_contract(session_factory, worker_settings, cipher, tenant)
    second = make_stored_contract(session_factory, worker_settings, cipher, tenant)

    result = worker_for(session_factory, worker_settings, extractor=BrokenExtractor()).run_once(
        now=MOMENT
    )

    assert result.unavailable == 2
    assert result.stamped == 0
    assert stamp_of(session_factory, first) is None
    assert stamp_of(session_factory, second) is None


def test_the_contract_a_down_backend_skipped_is_extracted_once_it_answers(
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
    cipher: FieldCipher,
    tenant: Tenant,
) -> None:
    """The retry is the whole reason ``unavailable`` is not stamped."""
    contract_id = make_stored_contract(session_factory, worker_settings, cipher, tenant)

    worker_for(session_factory, worker_settings, extractor=BrokenExtractor()).run_once(now=MOMENT)
    recovered = worker_for(session_factory, worker_settings).run_once(now=LATER)

    assert recovered.extracted == 1
    assert stamp_of(session_factory, contract_id) == LATER


# --- the cursor -----------------------------------------------------------
def test_the_cursor_carries_past_a_document_it_cannot_read(
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
    cipher: FieldCipher,
    tenant: Tenant,
) -> None:
    """One permanently unreadable document must not starve the ones behind it.

    With a batch of one, the first tick spends itself on the broken row; the second
    starts *after* it and finishes the good one. Without the ``after_id`` cursor both
    ticks would claim the same broken contract and the queue would never move.
    """
    broken = make_contract(session_factory, tenant, storage_path="not-a-real-key")
    good = make_stored_contract(session_factory, worker_settings, cipher, tenant)
    worker = worker_for(session_factory, worker_settings, batch_size=1)

    first = worker.run_once(now=MOMENT)
    second = worker.run_once(now=MOMENT)

    assert first.unreadable == 1
    assert second.extracted == 1
    assert stamp_of(session_factory, good) == MOMENT
    assert stamp_of(session_factory, broken) is None


def test_a_page_that_did_not_fill_sends_the_next_tick_back_to_the_start(
    session_factory: sessionmaker[Session], worker_settings: Settings, tenant: Tenant
) -> None:
    """A short page means the queue is exhausted, so the cursor wraps to zero.

    Observable through the unreadable row: it is still claimable, so a second tick must
    find it again. A cursor that kept its value would report an empty queue forever.
    """
    make_contract(session_factory, tenant, storage_path="not-a-real-key")
    worker = worker_for(session_factory, worker_settings, batch_size=10)

    worker.run_once(now=MOMENT)
    second = worker.run_once(now=MOMENT)

    assert second.candidates == 1
    assert second.unreadable == 1


def test_drain_walks_the_whole_queue_in_pages(
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
    cipher: FieldCipher,
    tenant: Tenant,
) -> None:
    """``--once`` has to empty the queue, which a single page of two cannot."""
    for _ in range(3):
        make_stored_contract(session_factory, worker_settings, cipher, tenant)

    result = worker_for(session_factory, worker_settings, batch_size=2).drain(now=MOMENT)

    assert result.candidates == 3
    assert result.extracted == 3
    assert result.backlog == 0


# --- tenancy --------------------------------------------------------------
def test_the_worker_is_a_system_actor_and_scans_every_tenant(
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
    cipher: FieldCipher,
    tenant: Tenant,
    other_tenant: Tenant,
) -> None:
    """Unlike every repository, this claim has no tenant predicate — deliberately.

    A worker has no principal to scope to. Scoping it would mean one process per tenant,
    and a new tenant would silently never have its contracts extracted. The isolation
    that matters is on the rows it writes: each field is stamped with its contract's
    tenant, which ``extract_contract`` does and this asserts.
    """
    mine = make_stored_contract(session_factory, worker_settings, cipher, tenant)
    theirs = make_stored_contract(session_factory, worker_settings, cipher, other_tenant)

    result = worker_for(session_factory, worker_settings).run_once(now=MOMENT)

    assert result.extracted == 2
    assert stamp_of(session_factory, mine) == MOMENT
    assert stamp_of(session_factory, theirs) == MOMENT
    assert fields_of(session_factory, mine)[FieldName.EMAIL].tenant_id == tenant.id
    assert fields_of(session_factory, theirs)[FieldName.EMAIL].tenant_id == other_tenant.id


# --- metrics --------------------------------------------------------------
def test_each_outcome_is_counted_under_its_own_label(
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
    cipher: FieldCipher,
    tenant: Tenant,
) -> None:
    make_stored_contract(session_factory, worker_settings, cipher, tenant)
    make_contract(session_factory, tenant, storage_path="not-a-real-key")

    worker_for(session_factory, worker_settings).run_once(now=MOMENT)

    assert metrics.CONTRACTS_EXTRACTED_TOTAL.value(outcome="extracted") == 1.0
    assert metrics.CONTRACTS_EXTRACTED_TOTAL.value(outcome="unreadable") == 1.0
    # One row is still queued, and the gauge is set from a count taken after the stamps.
    assert metrics.EXTRACTION_BACKLOG.value() == 1.0


def test_the_backlog_is_zero_once_the_queue_is_empty(
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
    cipher: FieldCipher,
    tenant: Tenant,
) -> None:
    make_stored_contract(session_factory, worker_settings, cipher, tenant)

    worker_for(session_factory, worker_settings).run_once(now=MOMENT)

    assert metrics.EXTRACTION_BACKLOG.value() == 0.0


def test_a_page_that_rolled_back_counts_nothing(
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
    cipher: FieldCipher,
    tenant: Tenant,
) -> None:
    """The metrics are incremented after the commit, and this is why.

    An unexpected failure (not ``ExtractionUnavailableError``) escapes ``_extract_one``,
    which rolls the page back. Counting during the page would leave the series naming a
    contract the worker still holds — the graph would claim work that never happened.
    """
    contract_id = make_stored_contract(session_factory, worker_settings, cipher, tenant)
    worker = worker_for(session_factory, worker_settings, extractor=DoomedExtractor())

    with pytest.raises(RuntimeError):
        worker.run_once(now=MOMENT)

    assert metrics.CONTRACTS_EXTRACTED_TOTAL.value(outcome="extracted") == 0.0
    assert metrics.CONTRACTS_EXTRACTED_TOTAL.value(outcome="empty") == 0.0
    assert metrics.EXTRACTION_BACKLOG.value() == 0.0
    assert stamp_of(session_factory, contract_id) is None


# --- the loop -------------------------------------------------------------
def test_run_forever_survives_a_tick_that_fails(
    session_factory: sessionmaker[Session], worker_settings: Settings
) -> None:
    """The one behaviour this worker owes an operator: it does not exit.

    A tick that raised would otherwise turn a transient database error into a dead
    container, and nothing would be extracting until a human noticed.
    """
    stop = threading.Event()
    worker = worker_for(session_factory, worker_settings)
    calls: list[int] = []

    def flaky_tick(*, now: datetime | None = None) -> ExtractionResult:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the database went away for one tick")
        stop.set()
        return ExtractionResult()

    worker.run_once = flaky_tick  # type: ignore[method-assign]

    worker.run_forever(stop)

    assert len(calls) == 2


def test_run_forever_finishes_when_stop_was_already_set(
    session_factory: sessionmaker[Session], worker_settings: Settings
) -> None:
    """``main`` sets the event from a signal handler before the first tick can matter."""
    stop = threading.Event()
    stop.set()
    worker = worker_for(session_factory, worker_settings)

    worker.run_forever(stop)

    assert metrics.EXTRACTION_BACKLOG.value() == 0.0
