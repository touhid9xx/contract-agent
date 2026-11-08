"""The scheduled attention check: gap 22's residue, and what "watching" means here.

``doctor`` turned the states nobody is told about into one number and one exit
code (``D10-36``). ``D10-39`` added a second caller with a different job —
:class:`~app.workers.attention_watch.AttentionWatch` — because a check nobody runs
finds nothing, and this stack ships a Compose file rather than a crontab.

The properties worth pinning are not "the worker starts". They are the four
decisions in that module, each of which could be made wrongly without failing a
test:

* **One line per tick, and its name carries the verdict.** The log line is this
  process's entire product; a loop that logged nothing, or logged three lines that
  each say part of it, would still "work".
* **A clean board is ``debug`` on purpose.** At five-minute intervals a clean board
  is the normal state, and a process that announces "nothing to do" forever is how
  the one line that matters gets filtered out. Asserted, because a well-meaning
  later change to ``info`` would be invisible in production and obvious in nobody's
  review.
* **A switched-off bus is said once, at startup, and not per tick.** With the
  default ``EVENT_TRANSPORT=none`` every claimed event parks, so a fresh install's
  first tick already carries parks. Saying that per tick is the noise this slice
  exists to avoid.
* **A failed tick is logged, not fatal.** A worker that dies on a transient
  connection error stops watching exactly when something is wrong.

What this file deliberately does **not** test is delivery. Nothing here reads the
log; the log is where the verdict goes, and the remaining half of gap 22 — making
it reach a person by itself — is open and blocked on a decision (``D10-39``).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence

import pytest
import structlog
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs
from structlog.typing import EventDict

from app.core.config import Settings
from app.core.crypto import FieldCipher
from app.db.base import utc_now
from app.db.enums import ContractStatus, FieldName, FieldStatus, OutboxStatus
from app.db.models import Contract, ExtractedField, OutboxEvent, Tenant
from app.db.session import session_scope
from app.workers.attention_watch import AttentionWatch

#: The value that must never appear on a log line, and the key that seals it.
PLAINTEXT_EMAIL = "a.rahman@example.test"
KEY = Fernet.generate_key().decode("ascii")
CIPHER = FieldCipher(KEY)

#: A transport that actually tries to deliver, so a park means "tried and failed"
#: rather than "the bus is switched off". Built as a real ``Settings`` so the
#: cross-field validation runs — a kafka transport with no broker is refused at
#: startup, which is the misconfiguration this constant must not be.
CONFIGURED = Settings(
    app_env="test",
    log_json=False,
    event_transport="kafka",
    kafka_bootstrap_servers="kafka:9092",
)


@pytest.fixture(autouse=True)
def _capture_at_debug() -> None:
    """Lower the capture threshold so the quiet verdict is observable.

    ``conftest`` pins structlog at ``INFO`` so no test can write to pytest's dying
    capture stream, and a clean board is logged at ``debug`` by design — so without
    this the line this worker emits most often would be the one the tests could not
    see, and "a clean board is quiet" would be untestable.

    No teardown, deliberately: ``quiet_logging`` is itself autouse, so it restores
    the pinned configuration after every test and this override cannot leak. That
    is why the teeth check — flipping the verdict line to ``info`` — turns this
    file red rather than silently passing.
    """
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))


# --- helpers ---------------------------------------------------------------
@pytest.fixture
def contract(session_factory: sessionmaker[Session], tenant: Tenant) -> Contract:
    with session_scope(session_factory) as session:
        row = Contract(
            tenant_id=tenant.id,
            customer_ref="A. Rahman",
            timezone="UTC",
            status=ContractStatus.ACTIVE,
        )
        session.add(row)
        session.flush()
        return row


def add_field(
    session_factory: sessionmaker[Session],
    contract: Contract,
    *,
    sealed: bool = False,
) -> int:
    """One extracted field, sealed or in the clear.

    The choice is the whole point: a plaintext row is a finding, a sealed one is
    the quiet case a "did anything get logged?" assertion would confuse with it.
    """
    with session_scope(session_factory) as session:
        row = ExtractedField(
            tenant_id=contract.tenant_id,
            contract_id=contract.id,
            field_name=FieldName.EMAIL,
            raw_value=CIPHER.encrypt(PLAINTEXT_EMAIL) if sealed else PLAINTEXT_EMAIL,
            normalized_value=CIPHER.encrypt(PLAINTEXT_EMAIL) if sealed else PLAINTEXT_EMAIL,
            confidence=0.93,
            status=FieldStatus.APPROVED,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def add_parked_event(session_factory: sessionmaker[Session], tenant: Tenant) -> int:
    """An outbox row that spent its retry budget: the shape the relay parks."""
    with session_scope(session_factory) as session:
        row = OutboxEvent(
            correlation_id="corr-watch",
            tenant_id=tenant.id,
            event_type="contract.ingested",
            topic="contract.events",
            partition_key="1",
            payload={"contract_id": 1},
            status=OutboxStatus.FAILED,
            attempts=5,
            next_attempt_at=utc_now(),
            last_error="broker refused",
        )
        session.add(row)
        session.flush()
        return int(row.id)


def events(captured: Sequence[EventDict]) -> list[str]:
    """The event names in the order they were logged.

    ``Sequence`` rather than ``list`` because ``capture_logs`` hands back its own
    list type and ``list`` is invariant — annotating this as ``list[dict[...]]``
    was a claim the caller could not satisfy.
    """
    return [str(entry["event"]) for entry in captured]


# --- one line per tick, and it names the verdict --------------------------
def test_a_clean_board_logs_one_debug_line_and_nothing_else(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    watch = AttentionWatch(session_factory, settings=settings)

    with capture_logs() as captured:
        report = watch.run_once()

    assert report.needs_attention is False
    assert events(captured) == ["attention_watch_clean"]
    assert captured[0]["log_level"] == "debug"


def test_a_plaintext_value_logs_as_an_error_with_the_report_attached(
    session_factory: sessionmaker[Session],
    settings: Settings,
    contract: Contract,
) -> None:
    """The finding is on the line, not only in the return value.

    A deployment reads the log, not the function's return value — so the counts go
    on the event, and the ids go with them to make ``reseal-fields --ids`` possible
    without a second query.
    """
    field_id = add_field(session_factory, contract)

    with capture_logs() as captured:
        AttentionWatch(session_factory, settings=settings).run_once()

    assert events(captured) == ["attention_watch_needs_attention"]
    assert captured[0]["log_level"] == "error"
    assert captured[0]["unsealed_field_ids"] == [field_id]
    assert captured[0]["unsealed_field_values"] == 1


def test_the_log_line_carries_ids_and_never_values(
    session_factory: sessionmaker[Session],
    settings: Settings,
    contract: Contract,
) -> None:
    """No PII on the line, even when the line is about PII.

    The check's subject is a stored plaintext value; a log line that quoted it
    would leak the very thing it exists to find, into the one place (a log) that is
    copied, shipped and retained.
    """
    add_field(session_factory, contract)

    with capture_logs() as captured:
        AttentionWatch(session_factory, settings=settings).run_once()

    assert PLAINTEXT_EMAIL not in str(captured)


# --- a switched-off bus is a fact, not a finding --------------------------
def test_a_park_under_a_switched_off_bus_is_logged_as_a_parking_notice(
    session_factory: sessionmaker[Session], settings: Settings, tenant: Tenant
) -> None:
    """Counted and named, but not raised as an error.

    ``EVENT_TRANSPORT=none`` parks every event by design, so this is the shape of a
    default installation. It is still said — the count is real and the events are
    not draining — but at ``info``, and it does not make the run a finding.
    """
    add_parked_event(session_factory, tenant)

    with capture_logs() as captured:
        report = AttentionWatch(session_factory, settings=settings).run_once()

    assert report.needs_attention is False
    assert events(captured) == ["attention_watch_bus_is_switched_off_parking"]
    assert captured[0]["log_level"] == "info"
    assert captured[0]["outbox_parked"] == 1
    assert captured[0]["outbox_parked_explained"] == 1


def test_a_park_under_a_configured_transport_is_an_error(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """Same row, same count, opposite verdict — the transport decides."""
    add_parked_event(session_factory, tenant)

    with capture_logs() as captured:
        report = AttentionWatch(session_factory, settings=CONFIGURED).run_once()

    assert report.needs_attention is True
    assert events(captured) == ["attention_watch_needs_attention"]
    assert captured[0]["log_level"] == "error"
    assert captured[0]["outbox_parked_explained"] == 0


# --- the startup announcement --------------------------------------------
def test_the_switched_off_bus_is_announced_once_at_startup(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    """Said before the first tick, and only once.

    Stopping before the loop body runs is the point: the announcement must not be a
    per-tick line, so a run with no ticks still produces it and produces nothing
    else.
    """
    stop = threading.Event()
    stop.set()

    with capture_logs() as captured:
        AttentionWatch(session_factory, settings=settings).run_forever(stop)

    assert events(captured) == [
        "attention_watch_started",
        "attention_watch_bus_is_switched_off",
        "attention_watch_stopped",
    ]


def test_a_configured_transport_does_not_warn_about_the_bus(
    session_factory: sessionmaker[Session],
) -> None:
    """The warning is conditional, or it is wallpaper."""
    stop = threading.Event()
    stop.set()

    with capture_logs() as captured:
        AttentionWatch(session_factory, settings=CONFIGURED).run_forever(stop)

    assert "attention_watch_bus_is_switched_off" not in events(captured)


# --- a truncated scan admits it ------------------------------------------
def test_a_full_scan_page_warns_that_the_count_is_a_floor(
    session_factory: sessionmaker[Session],
    settings: Settings,
    contract: Contract,
) -> None:
    """The truncation warning comes first, then the verdict.

    Ordered loudest-true-statement-first: "the number you are about to read is a
    floor" is only useful *before* the number.
    """
    add_field(session_factory, contract, sealed=True)

    with capture_logs() as captured:
        AttentionWatch(session_factory, settings=settings, limit=1).run_once()

    assert events(captured) == [
        "attention_watch_field_scan_truncated",
        "attention_watch_clean",
    ]
    assert captured[0]["log_level"] == "warning"


# --- a failed tick is a line, not a crash --------------------------------
def test_a_failed_tick_is_logged_and_the_loop_survives(
    session_factory: sessionmaker[Session],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database blip must not end the watching.

    ``restart: unless-stopped`` would bring the container back, but a crash loop
    buries the line that says what is wrong — the same reasoning that keeps a
    finding from exiting non-zero. The exception is logged with its traceback and
    the loop moves on.
    """
    watch = AttentionWatch(session_factory, settings=settings)
    stop = threading.Event()

    def _boom() -> None:
        stop.set()  # so the loop ends after this one failed tick
        raise RuntimeError("database went away")

    monkeypatch.setattr(watch, "run_once", _boom)

    with capture_logs() as captured:
        watch.run_forever(stop)  # returns normally; does not raise

    assert "attention_watch_tick_failed" in events(captured)
