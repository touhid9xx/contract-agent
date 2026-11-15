"""The scheduled attention check: gap 22's residue, and what "watching" means here.

``doctor`` turned the states nobody is told about into one number and one exit
code (``D10-36``). ``D10-39`` added a second caller with a different job —
:class:`~app.workers.attention_watch.AttentionWatch` — because a check nobody runs
finds nothing, and this stack ships a Compose file rather than a crontab.

The properties worth pinning are not "the worker starts". They are the five
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
* **The park count is published on every tick, the zero included** (``D10-117``).
  A family written only when something is wrong is indistinguishable, from
  Prometheus' side, from a process that stopped ticking — which is ``D10-109``'s
  defect one worker over. Both ``outcome`` series are set every tick, so the only
  way this worker's scrape stops carrying them is that it stopped.

What this file deliberately does **not** test is delivery: nothing here reads the
log. ``D10-117`` did not change that, and it did not reopen the question ``D10-39``
closed — it gave the park *count* a series, split by the explained/unexplained
distinction the verdict already made, so ``OutboxEventsParked`` has something to
read. A gauge is still scraped by the deployment rather than fetched by a tenant,
so *who may see a deployment-wide count* never became a question for a route, and
the tenant-facing board stays blocked on an owner's decision.
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

from app.core import metrics
from app.core.config import Settings
from app.core.crypto import FieldCipher
from app.core.metrics import OUTBOX_PARK_RELAY_FAILED, OUTBOX_PARK_TRANSPORT_DISABLED
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


@pytest.fixture(autouse=True)
def clean_registry():
    """Empty the registry around every test, as ``tests/test_metrics.py`` does.

    Not tidiness: the gauge is process-wide, so a park published here would still be
    set when a later file scrapes the same registry — and ``test_metrics.py`` asserts
    which families report before anything records one. Without this the suite would
    pass or fail on file ordering, and the failure would look like a metrics bug in
    a place that never touched parks.
    """
    metrics.reset()
    yield
    metrics.reset()


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


def parked() -> dict[str, float]:
    """The park gauge as a scrape reads it: ``outcome`` -> value, absent omitted.

    Parsed out of ``render()`` rather than read with ``value()``, because those two
    disagree in exactly the case that matters: ``value()`` answers ``0.0`` for a
    series that was never set, and *no series* is the state ``D10-109`` proved can
    make an alert unfireable. A helper that could not tell the two apart would pass
    on the defect.
    """
    prefix = f'{metrics.OUTBOX_PARKED_EVENTS.name}{{outcome="'
    found: dict[str, float] = {}
    for line in metrics.render().splitlines():
        if not line.startswith(prefix):
            continue
        outcome, _, value = line[len(prefix) :].partition('"}')
        found[outcome] = float(value)
    return found


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


# --- the park count, published -------------------------------------------
def test_the_family_is_declared_before_the_first_tick() -> None:
    """Declared and sample-free, which is a different state from a zero.

    A gauge has no single series to seed — ``outcome`` names them, and inventing
    outcomes here would publish numbers for paths no call site produces (the reason
    ``Counter._seed_its_one_series`` stops at unlabelled families). So before a tick
    the scrape carries the announcement and no number, and "absent cannot fire" is
    the one behaviour ``rules.test.yml`` case 6 pins rather than this file.
    """
    assert "# TYPE outbox_parked_events gauge" in metrics.render()
    assert parked() == {}


def test_a_clean_board_publishes_both_outcomes_at_zero(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    """The zero is published, per outcome, on a board with nothing to report.

    This is the test that fails if ``_publish_parks`` is ever moved inside an
    ``if report.outbox_parked`` branch: a clean install would then render no series,
    the rule would read "no data", and a watcher that had died would look exactly
    like a deployment with nothing parked. That is ``D10-109`` again, one worker over
    — the defect was a counter whose first increment was invisible, and its
    equivalent here is a gauge whose zero is.
    """
    AttentionWatch(session_factory, settings=settings).run_once()

    assert parked() == {
        OUTBOX_PARK_TRANSPORT_DISABLED: 0.0,
        OUTBOX_PARK_RELAY_FAILED: 0.0,
    }


def test_a_park_with_the_bus_switched_off_lands_on_transport_disabled(
    session_factory: sessionmaker[Session], settings: Settings, tenant: Tenant
) -> None:
    """The default install's parks are published as explained, not dropped.

    ``EVENT_TRANSPORT=none`` parks every event it claims, so this is what a fresh
    ``docker compose up`` looks like: one row parked, and the series that carries it
    is the one the alert does not read. The count stays on the wire — excluded from
    paging, not hidden from view.
    """
    add_parked_event(session_factory, tenant)

    AttentionWatch(session_factory, settings=settings).run_once()

    assert parked() == {
        OUTBOX_PARK_TRANSPORT_DISABLED: 1.0,
        OUTBOX_PARK_RELAY_FAILED: 0.0,
    }


def test_a_park_with_a_configured_transport_lands_on_relay_failed(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """Same row, same count, the other series — the transport decides, as it does for the verdict."""
    add_parked_event(session_factory, tenant)

    AttentionWatch(session_factory, settings=CONFIGURED).run_once()

    assert parked() == {
        OUTBOX_PARK_TRANSPORT_DISABLED: 0.0,
        OUTBOX_PARK_RELAY_FAILED: 1.0,
    }


def test_a_replayed_row_lowers_the_gauge(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """The reason it is a gauge: fixing the cause has to be able to silence the alert.

    ``outbox-replay`` un-parks a row, so the next tick must publish a smaller number —
    which ``rules.test.yml`` case 7 relies on when it asserts the alert resolves. A
    counter of "parks ever" would sit above zero for ever after the first one was
    cleared, and an alarm nobody can answer by doing the right thing is one that gets
    muted instead.
    """
    event_id = add_parked_event(session_factory, tenant)
    watch = AttentionWatch(session_factory, settings=CONFIGURED)

    watch.run_once()
    assert parked()[OUTBOX_PARK_RELAY_FAILED] == 1.0

    with session_scope(session_factory) as session:
        row = session.get(OutboxEvent, event_id)
        assert row is not None
        row.status = OutboxStatus.PUBLISHED

    watch.run_once()
    assert parked() == {
        OUTBOX_PARK_TRANSPORT_DISABLED: 0.0,
        OUTBOX_PARK_RELAY_FAILED: 0.0,
    }
