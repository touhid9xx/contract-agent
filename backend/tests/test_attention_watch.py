"""The scheduled attention check: gap 22's residue, and what "watching" means here.

``doctor`` turned the states nothing was publishing into one number and one exit
code (``D10-36``). ``D10-39`` added a second caller with a different job —
:class:`~app.workers.attention_watch.AttentionWatch` — because a check nobody runs
finds nothing, and this stack ships a Compose file rather than a crontab.

The properties worth pinning are not "the worker starts". They are the seven
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
* **The undelivered backlog rides the same tick, both statuses, zeros included**
  (``D10-118``). Same defect class, the other counter. Here the two labels are not
  explained-versus-unexplained but *fault versus verdict*: ``status="failed"`` is a
  transport that was configured and refused, so nothing in the deployment clears it
  by itself; ``status="suppressed"`` is this codebase deciding on purpose not to
  send, which is usually the system working. Both are published, and the shipped
  rule reads only the first — so the split has to be pinned, because a writer that
  put the same number on both series would still render plausible-looking numbers.
* **The queue that had no reading at all is now counted, and only one of its three
  causes may page** (``D10-129``). ``D10-118`` counted rows that *stopped*. A row
  still ``pending`` was accounted for by the scheduler's existence — an argument
  that fails on the shipped ``NOTIFICATION_TRANSPORT=none``, where the transport
  raises before ``attempts`` is incremented, so no reminder can ever reach the retry
  cap and every one of them stays ``pending`` for ever while the board reads clean.
  The population is split by *is there a setting whose change would deliver this?*:
  ``unconfigured`` (yes, and it is off — published, deliberately not a reason, or
  every default install would page from its first reminder), ``unsupported`` (no,
  because the channel sits outside ``NOTIFIABLE_CHANNELS`` — a ceiling, and empty
  on every configuration this build ships since ``D10-144``) and ``stuck`` (the
  channel works and the send path is not draining). Only the last two raise the
  verdict, and the grace window is what stops a deployment doing its job from
  looking like one that is stuck.
* **The population none of those three can see is the outbox's backlog**
  (``D10-134``). Every family above counts rows that *stopped*. A ``pending``
  outbox event never stopped either — it is waiting for the relay — and the relay's
  own listener answers "did this process start?", not "is it draining": a relay that
  is not running writes no family at all, which a scrape cannot tell from a target
  nobody configured. So the reading has to come from outside the relay, which is
  this worker. It is a gauge with **no verdict attached**, because a slow relay is
  not a wrong contract and must not page on a board that says the tenant's work needs
  the tenant; and it is **unlabelled**, unlike the park gauge, because the query
  excludes rows still serving a retry backoff rather than labelling them, so a
  non-zero reading has exactly one explanation.

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
from datetime import date, datetime, timedelta

import pytest
import structlog
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs
from structlog.typing import EventDict

from app.core import metrics
from app.core.config import ARI_NUMBER_PLACEHOLDER, Settings
from app.core.crypto import FieldCipher
from app.core.metrics import (
    NOTIFICATION_PENDING_STUCK,
    NOTIFICATION_PENDING_UNCONFIGURED,
    NOTIFICATION_PENDING_UNSUPPORTED,
    NOTIFICATION_UNDELIVERED_FAILED,
    NOTIFICATION_UNDELIVERED_SUPPRESSED,
    OUTBOX_PARK_RELAY_FAILED,
    OUTBOX_PARK_TRANSPORT_DISABLED,
)
from app.db.base import utc_now
from app.db.enums import (
    Channel,
    ContractStatus,
    FieldName,
    FieldStatus,
    NotificationStatus,
    OutboxStatus,
)
from app.db.models import Contract, ExtractedField, Notification, OutboxEvent, Tenant
from app.db.session import session_scope
from app.workers.attention_watch import AttentionWatch
from app.workers.dispatch import notification_idempotency_key

#: The value that must never appear on a log line, and the key that seals it.
PLAINTEXT_EMAIL = "a.rahman@example.test"
#: The ``SMTP_FROM`` value. Not a contact: it is the deployment's own address, and
#: nothing in this file sends anything — it exists because a valid smtp transport
#: is refused at start-up without one.
NOTIFICATION_SENDER_ADDRESS = "reminders@example.com"
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

#: A deployment that can actually send an email, so a queued reminder on that
#: channel means the send path has stopped rather than that nothing was wired.
#: Built as a real ``Settings`` for the same reason as ``CONFIGURED``: the
#: smtp-without-a-host combination is refused at start-up, and the value this
#: constant stands for is precisely a *valid* one.
DELIVERABLE = Settings(
    app_env="test",
    log_json=False,
    notification_transport="smtp",
    smtp_host="relay.example.com",
    smtp_from=NOTIFICATION_SENDER_ADDRESS,
)

#: The same deployment, plus a dialler — so ``voice`` is deliverable too, and only an
#: ``sms`` row is left with nothing behind it. Used by the split test, which needs one
#: row of each cause and therefore a configuration that produces ``unsupported`` for a
#: channel a switch *does* clear.
DELIVERABLE_WITH_A_DIALLER = Settings(
    app_env="test",
    log_json=False,
    notification_transport="smtp",
    smtp_host="relay.example.com",
    smtp_from=NOTIFICATION_SENDER_ADDRESS,
    voice_transport="ari",
    ari_base_url="http://127.0.0.1:8088",
    ari_username="ari-user",
    ari_password="ari_test_secret",
    ari_endpoint=f"PJSIP/{ARI_NUMBER_PLACEHOLDER}@trunk",
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


def add_unclaimed_event(
    session_factory: sessionmaker[Session], tenant: Tenant, *, waited: timedelta
) -> int:
    """A pending outbox row nothing has claimed, written ``waited`` ago.

    ``next_attempt_at`` is the timestamp the gauge reads, so ageing the row is a matter
    of writing it — no sleeping, and no clock to monkeypatch.
    """
    with session_scope(session_factory) as session:
        row = OutboxEvent(
            correlation_id="corr-watch",
            tenant_id=tenant.id,
            event_type="contract.ingested",
            topic="contract.events",
            partition_key="1",
            payload={"contract_id": 1},
            status=OutboxStatus.PENDING,
            attempts=0,
            next_attempt_at=utc_now() - waited,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def add_notification(
    session_factory: sessionmaker[Session],
    contract: Contract,
    status: NotificationStatus,
    *,
    template: str = "T-60",
    slot: date = date(2025, 1, 2),
    channel: Channel = Channel.EMAIL,
    scheduled_at: datetime | None = None,
    created_at: datetime | None = None,
) -> int:
    """One notification row in a chosen state, written directly.

    Reaching ``failed`` or ``suppressed`` through the real scheduler needs a refusing
    transport and a spent retry budget, and under the shipped
    ``NOTIFICATION_TRANSPORT=none`` the ``failed`` state is unreachable by design —
    the attempt counter is not incremented on that path, so the row stays ``pending``
    for ever. The state is the subject here, not the worker that would produce it, so
    the row is written the way ``test_doctor`` and ``test_attention_route`` write it.

    ``scheduled_at`` and ``created_at`` are the two clocks the overdue predicate reads,
    and they are supplied as values rather than derived from "now" so a case can sit
    on either side of the grace window without sleeping. ``None`` for both is the
    scheduler's own shape for "send as soon as you can" — see
    :func:`app.db.repositories.notifications._overdue_pending`, which ages such a row
    from ``created_at`` rather than ignoring it.
    """
    with session_scope(session_factory) as session:
        row = Notification(
            tenant_id=contract.tenant_id,
            contract_id=contract.id,
            channel=channel,
            template=template,
            status=status,
            scheduled_date=slot,
            scheduled_at=scheduled_at,
            idempotency_key=notification_idempotency_key(contract.id, template, slot),
            attempts=5 if status is NotificationStatus.FAILED else 0,
            last_error="relay refused" if status is NotificationStatus.FAILED else None,
        )
        if created_at is not None:
            # Assigned rather than passed: the column has an application-side
            # ``default``, and passing ``None`` explicitly would try to store NULL.
            row.created_at = created_at
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


def _series(family: str, label: str) -> dict[str, float]:
    """One labelled gauge as a scrape reads it: label value -> value, absent omitted.

    Parsed out of ``render()`` rather than read with ``value()``, because those two
    disagree in exactly the case that matters: ``value()`` answers ``0.0`` for a
    series that was never set, and *no series* is the state ``D10-109`` proved can
    make an alert unfireable. A helper that could not tell the two apart would pass
    on the defect.
    """
    prefix = f'{family}{{{label}="'
    found: dict[str, float] = {}
    for line in metrics.render().splitlines():
        if not line.startswith(prefix):
            continue
        key, _, value = line[len(prefix) :].partition('"}')
        found[key] = float(value)
    return found


def parked() -> dict[str, float]:
    """The park gauge, by ``outcome``."""
    return _series(metrics.OUTBOX_PARKED_EVENTS.name, "outcome")


def undelivered() -> dict[str, float]:
    """The undelivered-notification gauge, by ``status``."""
    return _series(metrics.NOTIFICATIONS_UNDELIVERED.name, "status")


def pending() -> dict[str, float]:
    """The overdue-pending gauge, by ``state``."""
    return _series(metrics.NOTIFICATIONS_PENDING_OVERDUE.name, "state")


def _unlabelled(family: str) -> float | None:
    """One unlabelled gauge as a scrape reads it, or ``None`` if it has no series.

    The helpers above cannot see this family at all: they build ``family{label="`` as
    their prefix, and a gauge declared without labels renders ``family 0``. Absence is
    reported as ``None`` rather than as zero for the reason those helpers parse
    ``render()`` instead of calling ``value()`` — "no series" and "a zero" are the two
    states an alert treats differently (``D10-109``), so a helper that conflated them
    could not catch the defect it exists for.
    """
    prefix = f"{family} "
    for line in metrics.render().splitlines():
        if line.startswith(prefix):
            return float(line[len(prefix) :])
    return None


def unclaimed_outbox() -> float | None:
    """The unclaimed-outbox gauge (``D10-134``)."""
    return _unlabelled(metrics.OUTBOX_PENDING_COUNT.name)


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

    This is the test that fails if ``_publish_gauges`` is ever moved inside an
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


# --- the undelivered backlog, published (D10-118) -------------------------
def test_a_clean_board_publishes_both_notification_statuses_at_zero(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    """The park zero's defect class, on the counter next door.

    Were these series set only when a row was stuck, a deployment where every message
    was delivered would render no series at all — and a watcher that had died would
    render the same nothing. An alert rule cannot fire on a series that does not
    exist, so ``rules.test.yml`` case 10's "absent cannot fire" is a different
    property and not a substitute for this one.
    """
    AttentionWatch(session_factory, settings=settings).run_once()

    assert undelivered() == {
        NOTIFICATION_UNDELIVERED_FAILED: 0.0,
        NOTIFICATION_UNDELIVERED_SUPPRESSED: 0.0,
    }


def test_a_suppressed_row_is_published_but_not_on_the_series_the_rule_reads(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """A policy verdict is counted and shown to the tenant, and deliberately not paged on.

    ``suppressed`` is this codebase deciding not to contact somebody — the contract
    closed, consent was withdrawn, a reviewer flagged the field. It is a person's job
    on ``/attention``, which is why the verdict raises it here at ``error``; and it is
    usually the system behaving correctly, which is why the shipped rule reads only
    ``status="failed"``. Both halves are asserted: keeping the count and dropping the
    page are each half the design, and dropping either would still leave a
    plausible-looking gauge.
    """
    add_notification(session_factory, contract, NotificationStatus.SUPPRESSED)

    with capture_logs() as captured:
        report = AttentionWatch(session_factory, settings=settings).run_once()

    assert undelivered() == {
        NOTIFICATION_UNDELIVERED_FAILED: 0.0,
        NOTIFICATION_UNDELIVERED_SUPPRESSED: 1.0,
    }
    assert report.needs_attention is True
    assert events(captured) == ["attention_watch_needs_attention"]


def test_a_failed_row_lands_on_the_series_the_rule_reads(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """A transport that refused is the deployment's fault, so it is the one that pages.

    Unreachable under the shipped ``NOTIFICATION_TRANSPORT=none``: the dispatcher
    raises ``SENDER_UNAVAILABLE`` before ``attempts`` moves, so the retry budget never
    runs out and no row arrives in ``failed`` however long a default install runs.
    That is exactly what makes the state mean "something refused" rather than "nobody
    configured a transport", and it is why the row is written directly here.
    """
    add_notification(session_factory, contract, NotificationStatus.FAILED)

    AttentionWatch(session_factory, settings=settings).run_once()

    assert undelivered() == {
        NOTIFICATION_UNDELIVERED_FAILED: 1.0,
        NOTIFICATION_UNDELIVERED_SUPPRESSED: 0.0,
    }


def test_the_two_states_are_counted_separately(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """Two rows in one state, one in the other, and the numbers must differ.

    The cheap mistake is publishing ``notifications_failed`` on both series. It
    renders a perfectly plausible gauge, and it makes the suppressed backlog invisible
    to the reader who is looking for it. Unequal counts are the only input that
    catches it — which is why this test does not use one row of each.
    """
    add_notification(session_factory, contract, NotificationStatus.FAILED, template="T-60")
    add_notification(session_factory, contract, NotificationStatus.FAILED, template="T-30")
    add_notification(session_factory, contract, NotificationStatus.SUPPRESSED, template="T-14")

    AttentionWatch(session_factory, settings=settings).run_once()

    assert undelivered() == {
        NOTIFICATION_UNDELIVERED_FAILED: 2.0,
        NOTIFICATION_UNDELIVERED_SUPPRESSED: 1.0,
    }


def test_a_retried_row_lowers_the_gauge(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """``POST /notifications/{id}/retry`` has to be able to silence the alert.

    The same reason the park gauge is a gauge: the rule's resolution depends on the
    number falling once the cause is fixed, exactly as ``OutboxEventsParked`` depends
    on ``outbox-replay``. A counter of "failures ever" would sit above zero for ever
    after the first one was answered, and an alarm nobody can answer by doing the
    right thing is the alarm that gets muted instead.
    """
    notification_id = add_notification(session_factory, contract, NotificationStatus.FAILED)
    watch = AttentionWatch(session_factory, settings=settings)

    watch.run_once()
    assert undelivered()[NOTIFICATION_UNDELIVERED_FAILED] == 1.0

    with session_scope(session_factory) as session:
        row = session.get(Notification, notification_id)
        assert row is not None
        row.status = NotificationStatus.PENDING
        row.attempts = 0

    watch.run_once()
    assert undelivered() == {
        NOTIFICATION_UNDELIVERED_FAILED: 0.0,
        NOTIFICATION_UNDELIVERED_SUPPRESSED: 0.0,
    }


# --- the queue that had no reading (D10-129) -----------------------------
#: How far in the past a row has to be to be unambiguously past the shipped
#: fifteen-minute grace. Well clear of it, so the case cannot flip on a slow runner
#: that takes a few seconds between writing the row and reading the report.
OVERDUE = timedelta(minutes=30)


def test_a_clean_board_publishes_all_three_pending_causes_at_zero(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    """``D10-109``'s defect class, on the third gauge.

    A series that is only written when something is wrong cannot be told apart, from
    the scrape's side, from a worker that stopped. The three zeros are what makes
    "nothing queued" a published fact rather than an absence.
    """
    AttentionWatch(session_factory, settings=settings).run_once()

    assert pending() == {
        NOTIFICATION_PENDING_UNCONFIGURED: 0.0,
        NOTIFICATION_PENDING_UNSUPPORTED: 0.0,
        NOTIFICATION_PENDING_STUCK: 0.0,
    }


def test_a_reminder_the_default_install_cannot_send_is_counted_and_excused(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """The population that made the board read clean while a queue sat behind it.

    This is the shipped configuration — ``NOTIFICATION_TRANSPORT=none`` — where the
    dispatcher raises before ``attempts`` moves, so the row can never reach the retry
    cap and sits ``pending`` for ever. It is counted, published, and deliberately
    **not** a finding: on a default install it is non-zero from the first reminder
    onward, and a verdict that fails every run of a correct deployment is a verdict
    that gets ignored.
    """
    assert settings.notification_transport == "none"
    add_notification(
        session_factory,
        contract,
        NotificationStatus.PENDING,
        scheduled_at=utc_now() - OVERDUE,
    )

    with capture_logs() as captured:
        report = AttentionWatch(session_factory, settings=settings).run_once()

    assert pending() == {
        NOTIFICATION_PENDING_UNCONFIGURED: 1.0,
        NOTIFICATION_PENDING_UNSUPPORTED: 0.0,
        NOTIFICATION_PENDING_STUCK: 0.0,
    }
    assert report.needs_attention is False
    assert events(captured) == ["attention_watch_clean"]


def test_a_voice_reminder_is_a_setting_s_fault_now_that_a_dialler_exists(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """``D10-144`` moved a queued voice row from ``unsupported`` to ``unconfigured``.

    Before it, no transport in this build placed a call, so the ceiling was the build
    and no environment variable cleared a voice row — which is why this file pinned it
    as *paged*. ``VOICE_TRANSPORT=ari`` is now a switch that clears it, so the row is
    excused exactly like an sms row on a default install: the operator's next move is a
    setting rather than a code change, and a verdict that fails every run of a correct
    deployment is a verdict that gets ignored.
    """
    add_notification(
        session_factory,
        contract,
        NotificationStatus.PENDING,
        channel=Channel.VOICE,
        scheduled_at=utc_now() - OVERDUE,
    )

    with capture_logs() as captured:
        report = AttentionWatch(session_factory, settings=settings).run_once()

    assert pending() == {
        NOTIFICATION_PENDING_UNCONFIGURED: 1.0,
        NOTIFICATION_PENDING_UNSUPPORTED: 0.0,
        NOTIFICATION_PENDING_STUCK: 0.0,
    }
    assert report.needs_attention is False
    assert events(captured) == ["attention_watch_clean"]


def test_a_voice_reminder_on_a_configured_dialler_is_stuck(
    session_factory: sessionmaker[Session], contract: Contract
) -> None:
    """The other side of the same move: with a dialler wired, silence is a fault.

    Nothing about the row changed — only the switch did — so this is the pair that
    shows the split reads configuration rather than the channel name.
    """
    assert Channel.VOICE in DELIVERABLE_WITH_A_DIALLER.deliverable_notification_channels
    add_notification(
        session_factory,
        contract,
        NotificationStatus.PENDING,
        channel=Channel.VOICE,
        scheduled_at=utc_now() - OVERDUE,
    )

    report = AttentionWatch(session_factory, settings=DELIVERABLE_WITH_A_DIALLER).run_once()

    assert pending() == {
        NOTIFICATION_PENDING_UNCONFIGURED: 0.0,
        NOTIFICATION_PENDING_UNSUPPORTED: 0.0,
        NOTIFICATION_PENDING_STUCK: 1.0,
    }
    assert report.needs_attention is True


def test_a_queued_reminder_on_a_working_channel_is_stuck_and_raises(
    session_factory: sessionmaker[Session], contract: Contract
) -> None:
    """The same row, the same count, the other cause — the transport decides.

    Under :data:`DELIVERABLE` this channel can be sent, so a row past the grace means
    the send path is not claiming it. This is the one cause of the three that no
    setting explains, and the only one that is a fault on every deployment.
    """
    add_notification(
        session_factory,
        contract,
        NotificationStatus.PENDING,
        scheduled_at=utc_now() - OVERDUE,
    )

    report = AttentionWatch(session_factory, settings=DELIVERABLE).run_once()

    assert pending() == {
        NOTIFICATION_PENDING_UNCONFIGURED: 0.0,
        NOTIFICATION_PENDING_UNSUPPORTED: 0.0,
        NOTIFICATION_PENDING_STUCK: 1.0,
    }
    assert report.needs_attention is True


def test_a_reminder_deferred_to_quiet_hours_is_counted_nowhere(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """Deferral is correct behaviour, and paging for it would page everyone nightly.

    A row parked for quiet hours carries a ``scheduled_at`` in the future, so it is not
    yet due and the repository predicate does not return it. This case exists because
    the cheap implementation — count ``pending`` and subtract the ones with an error —
    would include it, and the resulting alert would fire at two in the morning on every
    deployment that had configured quiet hours properly.
    """
    add_notification(
        session_factory,
        contract,
        NotificationStatus.PENDING,
        scheduled_at=utc_now() + timedelta(hours=3),
    )

    with capture_logs() as captured:
        report = AttentionWatch(session_factory, settings=settings).run_once()

    assert pending() == {
        NOTIFICATION_PENDING_UNCONFIGURED: 0.0,
        NOTIFICATION_PENDING_UNSUPPORTED: 0.0,
        NOTIFICATION_PENDING_STUCK: 0.0,
    }
    assert report.needs_attention is False
    assert events(captured) == ["attention_watch_clean"]


def test_a_reminder_inside_the_grace_window_is_not_yet_overdue(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """The grace window is what stops a working deployment looking like a stuck one.

    Without it, the seconds between a row becoming due and the scheduler's next tick
    would read as "the queue is not draining" on every single reminder — an alarm that
    fires when the system works is one that gets muted before the day it matters.
    """
    add_notification(
        session_factory,
        contract,
        NotificationStatus.PENDING,
        scheduled_at=utc_now() - timedelta(seconds=60),
    )

    AttentionWatch(session_factory, settings=DELIVERABLE).run_once()

    assert pending()[NOTIFICATION_PENDING_STUCK] == 0.0


def test_a_row_with_no_scheduled_time_is_aged_from_created_at(
    session_factory: sessionmaker[Session], settings: Settings, contract: Contract
) -> None:
    """``scheduled_at`` is nullable, and the scheduler treats NULL as due-now.

    So the age predicate has to fall back to ``created_at`` for exactly those rows.
    Reading ``scheduled_at`` alone would silently under-report the one shape the
    reminder planner writes for "send immediately" — the commonest row on a real
    install, and therefore the one whose invisibility matters most.
    """
    add_notification(
        session_factory,
        contract,
        NotificationStatus.PENDING,
        scheduled_at=None,
        created_at=utc_now() - OVERDUE,
    )

    AttentionWatch(session_factory, settings=settings).run_once()

    assert pending() == {
        NOTIFICATION_PENDING_UNCONFIGURED: 1.0,
        NOTIFICATION_PENDING_UNSUPPORTED: 0.0,
        NOTIFICATION_PENDING_STUCK: 0.0,
    }


def test_the_three_causes_are_counted_separately(
    session_factory: sessionmaker[Session], contract: Contract, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unequal counts, because equal ones pass on a writer that copies one number.

    An email row is ``stuck``, a voice row is ``unsupported`` and an sms row is
    ``unconfigured``: three causes, three different numbers. The cheap mistake —
    publishing one total on all three series — renders a perfectly plausible gauge
    and destroys the distinction the split exists for.

    ``D10-144`` closed the only route to ``unsupported`` a real deployment had (voice
    with no dialler), so the branch is exercised here the way the ceiling is meant to
    be read: a channel that leaves ``NOTIFIABLE_CHANNELS`` is unsupported **even though
    ``VOICE_TRANSPORT=ari`` makes it deliverable**. A copy of the classification that
    checked ``deliverable`` first would report ``stuck`` and fail this test, which is
    the ordering the function's docstring claims and would otherwise be unobservable.
    """
    monkeypatch.setattr(
        "app.core.attention.NOTIFIABLE_CHANNELS",
        frozenset({Channel.EMAIL, Channel.SMS}),
    )
    now = utc_now() - OVERDUE
    add_notification(
        session_factory, contract, NotificationStatus.PENDING, template="T-60", scheduled_at=now
    )
    add_notification(
        session_factory, contract, NotificationStatus.PENDING, template="T-30", scheduled_at=now
    )
    add_notification(
        session_factory,
        contract,
        NotificationStatus.PENDING,
        template="T-14",
        channel=Channel.VOICE,
        scheduled_at=now,
    )
    add_notification(
        session_factory,
        contract,
        NotificationStatus.PENDING,
        template="T-7",
        channel=Channel.SMS,
        scheduled_at=now,
    )

    AttentionWatch(session_factory, settings=DELIVERABLE_WITH_A_DIALLER).run_once()

    assert pending() == {
        NOTIFICATION_PENDING_UNCONFIGURED: 1.0,
        NOTIFICATION_PENDING_UNSUPPORTED: 1.0,
        NOTIFICATION_PENDING_STUCK: 2.0,
    }


# --- the outbox's other backlog, published (D10-134) ----------------------
#: Past the outbox lease, and well clear of it: the grace is a real setting on a real
#: ``Settings`` here, so a case has to be unambiguously beyond it rather than one
#: second past a number a slow runner might straddle.
def unclaimed_waited(settings: Settings) -> timedelta:
    """An age that is overdue by every reading of the shipped lease."""
    return timedelta(seconds=settings.outbox_lease_seconds + 60)


def test_a_clean_board_publishes_the_unclaimed_outbox_at_zero(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    """Published on a board with nothing to report, which is the whole point.

    ``D10-109``'s defect class one family over: a gauge written only when something
    is wrong renders no series when everything is fine, and a scrape cannot tell that
    apart from a watcher that stopped. That matters more here than anywhere else in
    this file, because this is the reading that survives the *observed* process
    dying — the relay writes nothing at all when it is not running, which is the
    reason an outside observer publishes this number instead (``D10-134``).
    """
    AttentionWatch(session_factory, settings=settings).run_once()

    assert unclaimed_outbox() == 0.0


def test_an_event_nothing_has_claimed_is_published_as_one(
    session_factory: sessionmaker[Session], settings: Settings, tenant: Tenant
) -> None:
    """The positive case, because a writer that always publishes zero passes the zero.

    One row the relay has not taken, aged past the lease. Nothing else is written, so
    the ``1.0`` can only have come from the row — and the row is ``pending``, which
    is a state no other family in this worker counts.
    """
    add_unclaimed_event(session_factory, tenant, waited=unclaimed_waited(settings))

    AttentionWatch(session_factory, settings=settings).run_once()

    assert unclaimed_outbox() == 1.0


def test_an_event_inside_the_lease_is_not_published(
    session_factory: sessionmaker[Session], settings: Settings, tenant: Tenant
) -> None:
    """A row the relay may still be about to claim is not evidence of anything.

    The grace is the same statement the relay's lease makes — how long a claim may
    stand — so a row newer than one lease is a row every relay has not yet had its
    full chance at. Without that exclusion the gauge would read non-zero in the
    seconds between a route committing an event and the relay's next poll, which is
    a number briefly true about every deployment and therefore about none.
    """
    add_unclaimed_event(session_factory, tenant, waited=timedelta(seconds=0))

    AttentionWatch(session_factory, settings=settings).run_once()

    assert unclaimed_outbox() == 0.0


def test_a_parked_row_moves_the_park_gauge_and_not_this_one(
    session_factory: sessionmaker[Session], settings: Settings, tenant: Tenant
) -> None:
    """Two families, two populations, and no row counted twice.

    A parked event is ``failed``: the relay spent its retry budget and stopped
    claiming it, which is a flag on ``outbox_parked_events``. Counting it here as
    well would make a dead-letter backlog look like a stalled relay, and those are
    two different jobs with two different answers — replay for the first, start the
    relay for the second. Asserted together in one tick, because the cheap mistake is
    a single count published on both families, which passes any test that reads one.
    """
    add_parked_event(session_factory, tenant)

    AttentionWatch(session_factory, settings=settings).run_once()

    assert unclaimed_outbox() == 0.0
    assert parked() == {
        OUTBOX_PARK_TRANSPORT_DISABLED: 1.0,
        OUTBOX_PARK_RELAY_FAILED: 0.0,
    }


def test_the_unclaimed_gauge_never_moves_the_verdict(
    session_factory: sessionmaker[Session], settings: Settings, tenant: Tenant
) -> None:
    """A relay that has stopped is not a tenant's contract needing a person.

    This is why the count is read beside the report rather than onto it
    (``AttentionReport``): a field there would either have to move
    ``needs_attention`` — sending an operator to look at contracts when the fault is
    a worker — or be a field no verdict reads. The board stays clean and the gauge
    still says one, so the fact is published without being misrouted.
    """
    add_unclaimed_event(session_factory, tenant, waited=unclaimed_waited(settings))

    with capture_logs() as captured:
        report = AttentionWatch(session_factory, settings=settings).run_once()

    assert unclaimed_outbox() == 1.0
    assert report.needs_attention is False
    assert events(captured) == ["attention_watch_clean"]
