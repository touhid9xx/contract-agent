"""The extraction worker: the clock that turns stored documents into field rows.

:func:`~app.workers.extraction.extract_contract` has been complete since F7 and enforces
the four rules that make extraction safe: never ``approved``, never over a reviewer's row,
always encrypt, one row per ``(contract, field)``. Nothing called it on a timer. The only
caller was ``python -m app.cli extract --contract-id N``, run by a person — so "the system
reads the contract you uploaded" was a promise the deployment kept by hand, and every
worker downstream (review queue, decision policy, reminder planner) was reading a table
that stayed empty until somebody remembered.

This module is that clock. It is deliberately the *smallest* possible one: it adds no new
extraction logic, no new parsing, and no new write path. It supplies the three things the
CLI supplied by hand — a selection predicate, a loop, and a deployment story.

Five decisions are worth stating, because each could be made the other way.

**The queue is a stamp, not a row count.** ``claim_extraction_candidates`` selects
``storage_path IS NOT NULL AND extraction_attempted_at IS NULL``, and this worker stamps
that column on the way out. The obvious predicate — "has a stored document and no
``extracted_fields`` rows" — cannot finish: a scanned page with no text layer, or a
contract that never states an e-mail, yields no row, so it would match on every tick for
ever. See ``0008_extraction_attempted_at``. The stamp commits in the *same transaction*
as the rows, because "we read it and ran extraction" is one fact; a stamp that survived a
rolled-back write would take the contract out of the queue for good.

**Four outcomes, and only three of them leave the queue.** ``extracted``, ``empty`` and
``reviewed`` mean the worker got as far as reading the document's text and running
extraction over it, so the work is done whatever it found. ``unreadable`` (bytes that will
not load, decrypt, parse, or that exceed
:data:`~app.core.config.Settings.extraction_max_document_chars`) and ``unavailable`` (the
backend was down) are *not* stamped: those are a volume, key-ring, build or configuration
problem, and the retry is a retry of the **reading**, which is the part a redeploy or a
restored key actually repairs. They cannot starve anything either, for the cursor reason
below.

**Progress comes from the id cursor, not from the predicate.** A contract this worker
cannot read keeps matching the claim on every tick, so with a batch smaller than that set,
ordering by anything stable would starve every contract behind it while the tick reported
itself healthy. Each page is claimed ``id > cursor`` and the cursor advances; a short or
empty page wraps it back to zero. This is the same fairness argument
:class:`~app.workers.reminder_planner.ReminderPlanner` makes, and the same code.

**Refusals are logged at debug, not warned, and never at error.** A contract awaiting a
repair will be refused on *every* tick for as long as it takes somebody to act; one
``error`` line per pass is how an operator learns to ignore the log. One ``info`` line
summarises a tick that advanced anything, and the per-contract line stays at ``debug``
with the contract's id and the *reason*. No document text, and no field value, is ever
logged — both are PII, which is why the CLI's own ``_stored_document_text`` helper is not
reused here (see :meth:`ExtractionWorker._document_text`).

**The whole page is one transaction, then committed.** Same shape as the planner: the
claim's ``FOR UPDATE SKIP LOCKED`` lease is held for the page, the cursor moves only after
the commit, and a failed page is re-read rather than skipped. The per-row cost here is
higher than any other worker's — a document load off the volume and a full parse — but a
page is :data:`~app.core.config.Settings.extraction_scan_batch_size` contracts every
fifteen minutes, not a hot loop, so the simpler transaction boundary is worth more than
the shorter lock.

Deliberately absent: an alert. A standing ``unreadable`` series is the one number here
that names work for a person, but whether it is newsworthy depends on how many documents
a deployment holds, and :data:`~app.core.metrics.EXTRACTION_BACKLOG` already falls when the
work is done. An operator reads the gauge; nothing pages on it.
"""

from __future__ import annotations

import argparse
import signal
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from types import FrameType

import structlog
from sqlalchemy.orm import Session, sessionmaker

from app.core.blind_index import ContactBlindIndex, build_blind_index
from app.core.config import Settings, get_settings
from app.core.crypto import DecryptionError, FieldCipher, build_cipher
from app.core.document_text import DocumentTextError, extract_document_text
from app.core.documents import DocumentStoreError, build_document_store
from app.core.logging import configure_logging
from app.core.metrics import (
    CONTRACTS_EXTRACTED_TOTAL,
    EXTRACTION_BACKLOG,
    EXTRACTION_OUTCOME_EMPTY,
    EXTRACTION_OUTCOME_EXTRACTED,
    EXTRACTION_OUTCOME_REVIEWED,
    EXTRACTION_OUTCOME_UNAVAILABLE,
    EXTRACTION_OUTCOME_UNREADABLE,
)
from app.db.base import utc_now
from app.db.models import Contract
from app.db.repositories.contracts import (
    claim_extraction_candidates,
    count_extraction_backlog,
)
from app.db.session import build_engine, build_session_factory, session_scope
from app.workers.extraction import (
    DocumentExtractor,
    ExtractionOutcome,
    ExtractionUnavailableError,
    extract_contract,
)
from app.workers.extractors import build_extractor
from app.workers.metrics_server import publish_metrics

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """What one page (or one drained run) did, in counts an operator can act on.

    Returned rather than only logged so a test can assert the verdict instead of scraping
    a log line. ``backlog`` is the queue *after* this page, which is the number that makes
    the other five readable: ``unreadable == 3`` means something different when the
    backlog is 4 than when it is 400.
    """

    candidates: int = 0
    extracted: int = 0
    empty: int = 0
    reviewed: int = 0
    #: Leased, but left in the queue: bytes this build cannot read, or a document over
    #: ``extraction_max_document_chars``.
    unreadable: int = 0
    #: Leased, but left in the queue: the extraction backend was down. Retried next tick.
    unavailable: int = 0
    #: Contracts still waiting to be extracted, as of the end of this page.
    backlog: int = 0

    @property
    def stamped(self) -> int:
        """Contracts that left the queue on this page."""
        return self.extracted + self.empty + self.reviewed

    @property
    def worked(self) -> bool:
        """Whether the pass advanced the queue. A page of only refusals did not."""
        return self.stamped > 0

    def merge(self, other: ExtractionResult) -> ExtractionResult:
        """Fold another page's result in, for :meth:`ExtractionWorker.drain`.

        ``backlog`` is *replaced* rather than summed: it is a reading of the queue at an
        instant, not an event, and adding two readings together would invent a number.
        """
        return ExtractionResult(
            candidates=self.candidates + other.candidates,
            extracted=self.extracted + other.extracted,
            empty=self.empty + other.empty,
            reviewed=self.reviewed + other.reviewed,
            unreadable=self.unreadable + other.unreadable,
            unavailable=self.unavailable + other.unavailable,
            backlog=other.backlog,
        )


class ExtractionWorker:
    """Extracts the stored documents nobody has extracted yet, one page at a time."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: Settings,
        batch_size: int | None = None,
        poll_interval_seconds: float | None = None,
        cipher: FieldCipher | None = None,
        contact_index: ContactBlindIndex | None = None,
        extractor: DocumentExtractor | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._batch_size = settings.extraction_scan_batch_size if batch_size is None else batch_size
        self._poll_interval = (
            settings.extraction_scan_poll_interval_seconds
            if poll_interval_seconds is None
            else poll_interval_seconds
        )
        # Built once, here, so a deployment missing a key fails at start-up rather than
        # once per contract. `extract_contract` refuses to run without both — an
        # unindexed contact value is one an inbound STOP can never find — and a worker
        # that discovered that on its first tick would look like a worker with nothing
        # to do. The CLI makes the same two refusals, for the same reason.
        self._cipher = build_cipher(settings) if cipher is None else cipher
        self._contact_index = (
            build_blind_index(settings) if contact_index is None else contact_index
        )
        self._extractor = build_extractor(settings) if extractor is None else extractor
        # Where the next page starts. Process memory on purpose: it is a fairness cursor,
        # not a fact about the data, so a restart resetting it to zero costs one repeated
        # page and nothing else.
        self._cursor = 0

    def run_once(self, *, now: datetime | None = None) -> ExtractionResult:
        """One page of queued documents, extracted in a single transaction."""
        return self._extract_page(utc_now() if now is None else now)

    def drain(self, *, now: datetime | None = None) -> ExtractionResult:
        """Every queued document, once, favouring a caller that runs and exits.

        For a cron-style ``--once`` this must cover the whole table: one page per run
        would be the same starvation the cursor exists to prevent, just moved from the
        loop to the scheduler. For the long-running loop it is not used, because a tick
        should bound its own work.
        """
        moment = utc_now() if now is None else now
        total = ExtractionResult()
        while True:
            total = total.merge(self._extract_page(moment))
            if self._cursor == 0:  # a short or empty page: this was the last one
                return total

    def run_forever(self, stop: threading.Event) -> None:
        """Poll until ``stop`` is set. Intended as the process main loop."""
        log.info(
            "extraction_worker_started",
            batch_size=self._batch_size,
            poll_interval_seconds=self._poll_interval,
            backend=self._settings.extraction_backend,
        )

        while not stop.is_set():
            try:
                result = self.run_once()
            except Exception:
                # A transient database fault must not kill the worker. Nothing was
                # committed for the failed page, so the next tick re-reads the same
                # contracts — the cursor only moves after a page commits.
                log.exception("extraction_worker_tick_failed")
                result = None

            if result is not None:
                self._log_tick(result)

            # Always wait. A quiet tick found nothing to do, and a busy one just extracted
            # a page; waiting zero would spin over a table that has not changed.
            if stop.wait(self._poll_interval):
                break

        log.info("extraction_worker_stopped")

    def _extract_page(self, moment: datetime) -> ExtractionResult:
        """Claim one page of queued contracts and extract every one of them."""
        counts: dict[str, int] = {}

        with session_scope(self._session_factory) as session:
            contracts = claim_extraction_candidates(
                session, after_id=self._cursor, limit=self._batch_size
            )
            for contract in contracts:
                outcome = self._extract_one(session, contract, moment)
                counts[outcome] = counts.get(outcome, 0) + 1
            # Advanced inside the transaction but applied only after it commits, below:
            # a rolled-back page must be re-read, not skipped.
            next_cursor = contracts[-1].id if len(contracts) == self._batch_size else 0
            # Read before the commit, in this session, so the gauge describes the queue
            # *after* this page's stamps — the only reading that answers "is the worker
            # keeping up", rather than "was there ever a backlog". That reading is only
            # correct because `count_extraction_backlog` flushes first: the stamps above
            # are pending ORM state, and this session does not autoflush.
            backlog = count_extraction_backlog(session)

        self._cursor = next_cursor

        # Counted after the commit. A page that rolled back would otherwise leave its
        # contracts in the series, and the graph would report work the queue still holds.
        for outcome, count in counts.items():
            CONTRACTS_EXTRACTED_TOTAL.inc(count, outcome=outcome)
        EXTRACTION_BACKLOG.set(backlog)

        return ExtractionResult(
            candidates=len(contracts),
            extracted=counts.get(EXTRACTION_OUTCOME_EXTRACTED, 0),
            empty=counts.get(EXTRACTION_OUTCOME_EMPTY, 0),
            reviewed=counts.get(EXTRACTION_OUTCOME_REVIEWED, 0),
            unreadable=counts.get(EXTRACTION_OUTCOME_UNREADABLE, 0),
            unavailable=counts.get(EXTRACTION_OUTCOME_UNAVAILABLE, 0),
            backlog=backlog,
        )

    def _extract_one(self, session: Session, contract: Contract, moment: datetime) -> str:
        """Extract one contract's stored document, returning its outcome token.

        Never raises for anything the document or the backend can do to us: every one of
        those is an outcome the caller counts, because a single unreadable document must
        not cost the rest of the page.
        """
        text = self._document_text(contract)
        if text is None:
            return EXTRACTION_OUTCOME_UNREADABLE

        # Refuse rather than truncate, exactly as the CLI does. A truncated document
        # produces missing fields, and "missing" is indistinguishable from "the contract
        # never stated it" — the ambiguity the review queue exists to resolve, so it must
        # not be manufactured here. Left unstamped: the fix is a setting, and the retry
        # is the point.
        limit = self._settings.extraction_max_document_chars
        if len(text) > limit:
            log.debug(
                "extraction_worker_document_too_long",
                contract_id=contract.id,
                characters=len(text),
                limit=limit,
            )
            return EXTRACTION_OUTCOME_UNREADABLE

        try:
            outcome = extract_contract(
                session,
                contract,
                text,
                extractor=self._extractor,
                cipher=self._cipher,
                contact_index=self._contact_index,
                review_threshold=self._settings.extraction_review_threshold,
            )
        except ExtractionUnavailableError as exc:
            # A backend that is down is a transient condition the whole page shares, and
            # the contract keeps its place for the next tick. Not fatal: the loop must
            # survive it, which is the one behaviour this worker owes an operator.
            log.debug(
                "extraction_worker_backend_unavailable",
                contract_id=contract.id,
                backend=self._settings.extraction_backend,
                error=str(exc),
            )
            return EXTRACTION_OUTCOME_UNAVAILABLE

        # In the same transaction as the rows above, and only after they are written.
        contract.extraction_attempted_at = moment

        verdict = self._classify(outcome)
        log.debug(
            "extraction_worker_extracted",
            contract_id=contract.id,
            tenant_id=str(contract.tenant_id),
            outcome=verdict,
            candidates=outcome.candidates,
            created=outcome.created,
            updated=outcome.updated,
            skipped_reviewed=outcome.skipped_reviewed,
            discarded=outcome.discarded,
        )
        return verdict

    @staticmethod
    def _classify(outcome: ExtractionOutcome) -> str:
        """Which of the three finishing outcomes a completed run was.

        ``extracted`` wins whenever a row was written or rewritten — the interesting
        fact about a run is that it stored something. Only when it stored nothing does
        the difference between "the reviewer owns every field" and "the document said
        nothing we keep" matter, and it matters to an operator: the first needs no
        action and the second may mean the document is a scan.
        """
        if outcome.stored > 0:
            return EXTRACTION_OUTCOME_EXTRACTED
        if outcome.skipped_reviewed > 0:
            return EXTRACTION_OUTCOME_REVIEWED
        return EXTRACTION_OUTCOME_EMPTY

    def _document_text(self, contract: Contract) -> str | None:
        """The contract's stored document as text, or ``None`` once the reason is logged.

        Deliberately a second, shorter reading of ``app.cli._stored_document_text`` rather
        than a call to it, and the difference is audience, not rules. That helper logs its
        refusals at ``error``, which is right for a command a person just typed and wrong
        for a pass that runs every fifteen minutes over the same broken document. It also
        returns ``None`` for all four failures, where this worker must distinguish "cannot
        read these bytes" (:data:`EXTRACTION_OUTCOME_UNREADABLE`) from "the backend is
        down" (:data:`EXTRACTION_OUTCOME_UNAVAILABLE`) — one is repaired by an operator and
        the other by waiting.
        """
        storage_path = contract.storage_path
        if storage_path is None:
            # The claim predicate excludes this, so reaching it means the row was edited
            # under the page. Counted as unreadable and left in the queue.
            log.debug("extraction_worker_no_stored_document", contract_id=contract.id)
            return None

        try:
            store = build_document_store(self._settings, cipher=self._cipher)
            payload = store.load(storage_path)
        except (DocumentStoreError, DecryptionError) as exc:
            log.debug(
                "extraction_worker_document_unreadable",
                contract_id=contract.id,
                error=str(exc),
            )
            return None

        try:
            extracted = extract_document_text(payload)
        except DocumentTextError as exc:
            log.debug(
                "extraction_worker_document_unsupported",
                contract_id=contract.id,
                error=str(exc),
            )
            return None
        return extracted.text

    def _log_tick(self, result: ExtractionResult) -> None:
        """One line per tick, and nothing at all when the queue was empty.

        A tick that moved contracts out of the queue is news. A tick that found them all
        unreadable is a standing condition and stays at ``debug`` — the same reading
        ``attention_watch`` takes of a clean board, so a healthy deployment's log is as
        quiet as its dashboard is flat.
        """
        if not result.candidates:
            return
        logger = log.info if result.worked else log.debug
        logger(
            "extraction_worker_tick",
            candidates=result.candidates,
            extracted=result.extracted,
            empty=result.empty,
            reviewed=result.reviewed,
            unreadable=result.unreadable,
            unavailable=result.unavailable,
            backlog=result.backlog,
        )


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m app.workers.extraction_worker [--once]``.

    ``--once`` drains every queued document and exits, for a deployment that would rather
    run the scan from cron than hold a process open. Without it the worker polls, which is
    how every other scanner in this codebase is deployed.
    """
    parser = argparse.ArgumentParser(prog="python -m app.workers.extraction_worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="extract every queued document once and exit, instead of polling",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.json_logs)

    engine = build_engine(settings)
    factory = build_session_factory(engine)
    worker = ExtractionWorker(factory, settings=settings)

    try:
        if args.once:
            result = worker.drain()
            log.info(
                "extraction_worker_drained",
                candidates=result.candidates,
                extracted=result.extracted,
                empty=result.empty,
                reviewed=result.reviewed,
                unreadable=result.unreadable,
                unavailable=result.unavailable,
                backlog=result.backlog,
            )
            return 0

        stop = threading.Event()

        def _request_stop(signum: int, _frame: FrameType | None) -> None:
            log.info("extraction_worker_stopping", signal=signum)
            stop.set()

        # Graceful shutdown: finish the page in flight, then exit. A page is one
        # transaction, so "finish" means "commit or roll back", never "half wrote".
        signal.signal(signal.SIGINT, _request_stop)
        signal.signal(signal.SIGTERM, _request_stop)
        # Served on the polling path only: a ``--once`` run finishes in seconds and must
        # not fight a long-running worker for the same port.
        with publish_metrics(settings, worker="extraction"):
            worker.run_forever(stop)
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
