"""The scheduled retention pass: gap 25's last residue, and what "off" has to mean.

``D10-148`` gave the outbox a retention command and left the residue in writing:
*nothing schedules it*. On every deployment before this slice the table therefore
grew for ever unless somebody remembered a command, and a ``none``-transport
install — the shipped default — parks one row per business event and never gives
any of them back (``D10-131``).

The module under test is deliberately only the scheduler: it invents no window and
carries no policy of its own. That makes the properties worth pinning narrow and
specific, each one something a plausible implementation could get wrong without
failing any other test in this suite:

* **Off means no query at all**, not "a query that deletes nothing". ``0`` is the
  shipped default, so the difference decides whether a stock install's table is
  untouched or merely lucky. Pinned by replacing ``prune_terminal_before`` with a
  function that raises: a "disabled" pass that still built a statement would turn
  this file red.
* **The cut-off is arithmetic on an injected clock.** ``before = now - days`` and
  the boundary is inclusive, because ``published_at <= before`` is the repository's
  predicate and a half-open window would keep a row exactly at the edge for ever.
  The clock is a constructor argument precisely so this can be asserted without
  sleeping.
* **Only settled rows can be reached.** ``pending`` and ``publishing`` are absent
  from the predicate rather than filtered afterwards — the one thing the outbox
  exists to make impossible is losing an undelivered event — and a parked row needs
  a second switch because a person was supposed to read it.
* **A full page is a floor, not a total.** One page per tick would drain a large
  backlog at 1 000 rows a day; the pass repeats until a page comes back short, and
  admits when the page bound was why it stopped.
* **The empty pass is logged.** A daily worker's line is its heartbeat, so "found
  nothing" is said at ``info`` rather than swallowed, and a failed pass is a line
  rather than a crash — ``restart: unless-stopped`` would restart the container but
  bury the reason in a crash loop.

What this file does not test is the deletion statement itself: ``D10-148`` pinned
``prune_terminal_before`` where it lives, and this worker reuses it rather than
restating the predicate, so the two cannot drift.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs
from structlog.typing import EventDict

from app.core.config import Settings
from app.db.enums import OutboxStatus
from app.db.models import OutboxEvent, Tenant
from app.db.session import session_scope
from app.workers.outbox_pruner import OutboxPruner, PruneReport

#: The clock every pass in this file reads. A fixed value rather than ``utc_now()``
#: so "published eight days ago" is a statement about the row, not about how long
#: the test took to run.
NOW = datetime(2026, 1, 15, 12, 0, 0)

#: The retention window used by every enabled case. Seven days, because it is the
#: number ``.env.example`` does not ship — a test that used the documented default
#: would pass on a pruner that ignored its setting entirely.
RETENTION = 7

#: The cut-off an enabled pass derives from :data:`NOW` and :data:`RETENTION`.
BEFORE = NOW - timedelta(days=RETENTION)


@pytest.fixture(autouse=True)
def _capture_at_info() -> None:
    """Pin the capture threshold at ``info``.

    ``conftest`` already does this; restated here so the file states the level its
    assertions depend on. Both log lines this worker emits are ``info`` — the empty
    pass included, because a daily worker's line is the only evidence its timer is
    alive.
    """
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(20))


# --- helpers ---------------------------------------------------------------
def add_published(
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    *,
    published_at: datetime,
) -> int:
    """A row whose work finished, stamped with the instant it did.

    ``published_at`` is the column the deletion predicate reads, and it is supplied
    rather than derived from "now" so a case can sit on either side of the cut-off
    without sleeping.
    """
    with session_scope(session_factory) as session:
        row = OutboxEvent(
            correlation_id="corr-prune",
            tenant_id=tenant.id,
            event_type="contract.ingested",
            topic="contract.events",
            partition_key="1",
            payload={"contract_id": 1},
            status=OutboxStatus.PUBLISHED,
            attempts=1,
            next_attempt_at=published_at,
            published_at=published_at,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def add_settled_event(
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    status: OutboxStatus,
    *,
    settled_at: datetime | None = None,
) -> int:
    """One row in a chosen state, written directly.

    Reaching ``published`` or ``failed`` through the real relay needs a transport
    and a spent retry budget, and under the shipped ``EVENT_TRANSPORT=none`` every
    claimed event parks. The state is the subject here rather than the worker that
    would produce it, so the row is written the way ``test_attention_watch`` writes
    its parked rows.

    ``updated_at`` is assigned rather than passed because the column carries an
    application-side ``default``: it is the timestamp the ``failed`` branch of the
    predicate reads, so ageing a parked row is a matter of writing it.
    """
    with session_scope(session_factory) as session:
        row = OutboxEvent(
            correlation_id="corr-prune",
            tenant_id=tenant.id,
            event_type="contract.ingested",
            topic="contract.events",
            partition_key="1",
            payload={"contract_id": 1},
            status=status,
            attempts=5 if status is OutboxStatus.FAILED else 0,
            next_attempt_at=settled_at or NOW,
            published_at=settled_at if status is OutboxStatus.PUBLISHED else None,
            last_error="broker refused" if status is OutboxStatus.FAILED else None,
        )
        if settled_at is not None:
            row.updated_at = settled_at
        session.add(row)
        session.flush()
        return int(row.id)


def remaining(session_factory: sessionmaker[Session]) -> list[int]:
    """Every outbox id still in the table, in id order."""
    with session_scope(session_factory) as session:
        return list(session.execute(select(OutboxEvent.id).order_by(OutboxEvent.id)).scalars())


def events(captured: Sequence[EventDict]) -> list[str]:
    """The event names in the order they were logged."""
    return [str(entry["event"]) for entry in captured]


def pruner(
    session_factory: sessionmaker[Session],
    settings: Settings,
    **overrides: object,
) -> OutboxPruner:
    """A pruner reading :data:`NOW` and the seven-day window unless overridden."""
    options: dict[str, object] = {"retention_days": RETENTION, **overrides}
    return OutboxPruner(
        session_factory,
        settings=settings,
        clock=lambda: NOW,
        **options,  # type: ignore[arg-type]
    )


# --- off means off --------------------------------------------------------
def test_a_disabled_pruner_reads_no_row_and_deletes_nothing(
    session_factory: sessionmaker[Session],
    settings: Settings,
    tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OUTBOX_RETENTION_DAYS=0`` must not so much as query the table.

    This is the shipped default, so the difference between "off" and "a pass that
    happens to match nothing" is the difference between every stock install keeping
    its history and every stock install deleting it the day a predicate is
    widened. ``prune_terminal_before`` is replaced with a function that raises: a
    disabled pass that built a statement at all would fail here.
    """
    assert settings.outbox_retention_days == 0
    event_id = add_published(session_factory, tenant, published_at=NOW - timedelta(days=3650))

    def _boom(**_kwargs: object) -> None:
        raise AssertionError("a disabled pruner must not query the outbox")

    monkeypatch.setattr("app.workers.outbox_pruner.prune_terminal_before", _boom)

    report = OutboxPruner(session_factory, settings=settings, clock=lambda: NOW).run_once()

    assert report.disabled is True
    assert report.pruned == ()
    assert report.count == 0
    assert report.before == NOW
    assert remaining(session_factory) == [event_id]


def test_a_disabled_pass_says_nothing_at_all(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    """No line per tick: the startup announcement is the whole of what "off" says.

    ``run_once`` returns its disabled report before the reporting step, so a
    switched-off pruner is silent — deliberately, because the alternative is a
    daily "nothing happened" for the life of the deployment. What keeps that from
    being an unobservable worker is the once-only line ``run_forever`` emits
    before its loop, pinned by
    :func:`test_retention_off_is_announced_once_at_startup_and_never_per_tick`.
    """
    with capture_logs() as captured:
        report = pruner(session_factory, settings, retention_days=0).run_once()

    assert report.disabled is True
    assert captured == []


# --- the window comes from the setting ------------------------------------
def test_the_window_is_read_from_the_setting_when_no_argument_overrides_it(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """The wiring, asserted end to end: config in, cut-off out.

    Every other case passes ``retention_days`` explicitly so it can fix the number;
    this one passes nothing, so it fails if the constructor stops reading
    ``Settings`` and the worker silently prunes on some default of its own.
    """
    configured = Settings(app_env="test", log_json=False, outbox_retention_days=RETENTION)
    inside = add_published(session_factory, tenant, published_at=BEFORE - timedelta(days=1))
    outside = add_published(session_factory, tenant, published_at=BEFORE + timedelta(days=1))

    report = OutboxPruner(session_factory, settings=configured, clock=lambda: NOW).run_once()

    assert report.before == BEFORE
    assert [event.id for event in report.pruned] == [inside]
    assert remaining(session_factory) == [outside]


def test_the_cut_off_is_inclusive_at_the_boundary(
    session_factory: sessionmaker[Session], settings: Settings, tenant: Tenant
) -> None:
    """A row that settled exactly ``days`` ago is already past retention.

    ``published_at <= before`` is the repository's predicate, and a half-open window
    here would keep a row sitting exactly on the edge for ever — one row per tick
    that never ages out, which is the shape of a retention pass that looks right and
    is not. ``PruneReport.before`` is the value the comparison is made against, so
    the arithmetic is asserted on the report rather than inferred from deletions.
    """
    edge = add_published(session_factory, tenant, published_at=BEFORE)
    later = add_published(session_factory, tenant, published_at=BEFORE + timedelta(seconds=1))

    report = pruner(session_factory, settings).run_once()

    assert report.before == BEFORE
    assert [event.id for event in report.pruned] == [edge]
    assert remaining(session_factory) == [later]


# --- only settled rows ----------------------------------------------------
def test_an_undelivered_row_is_outside_the_predicate_not_filtered_out_of_it(
    session_factory: sessionmaker[Session], settings: Settings, tenant: Tenant
) -> None:
    """``pending`` and ``publishing`` survive an ancient, unmistakeable age.

    Both rows here are older than the window by a year, so nothing about time
    protects them: they survive because the deletion predicate does not name their
    state. That is the difference between a retention pass and a retention pass with
    a filter in front of it — the first cannot be widened by a caller, the second
    can.
    """
    ancient = NOW - timedelta(days=365)
    published = add_published(session_factory, tenant, published_at=ancient)
    pending = add_settled_event(session_factory, tenant, OutboxStatus.PENDING, settled_at=ancient)
    publishing = add_settled_event(
        session_factory, tenant, OutboxStatus.PUBLISHING, settled_at=ancient
    )

    report = pruner(session_factory, settings).run_once()

    assert [event.id for event in report.pruned] == [published]
    assert remaining(session_factory) == [pending, publishing]


def test_a_parked_row_needs_the_second_switch(
    session_factory: sessionmaker[Session], settings: Settings, tenant: Tenant
) -> None:
    """``failed`` survives the default pass, and goes when the switch is set.

    Both directions in one test, because the cheap implementation — including
    ``failed`` unconditionally — passes either half alone. A parked row is one a
    person was meant to read and replay (``D7-01``), and a deletion is the one
    retention decision that cannot be undone, so it is a separate keystroke rather
    than a wider default.
    """
    parked = add_settled_event(
        session_factory, tenant, OutboxStatus.FAILED, settled_at=NOW - timedelta(days=30)
    )

    first = pruner(session_factory, settings).run_once()
    assert first.pruned == ()
    assert remaining(session_factory) == [parked]

    second = pruner(session_factory, settings, include_failed=True).run_once()
    assert [event.id for event in second.pruned] == [parked]
    assert remaining(session_factory) == []


def test_the_report_names_what_it_deleted_before_it_stops_existing(
    session_factory: sessionmaker[Session], settings: Settings, tenant: Tenant
) -> None:
    """A :class:`PruneReport` carries the row, not just a count.

    Once the ``DELETE`` runs there is nothing left to describe, so an operator's
    "how much of which tenant's history did I just remove" has to be answered from
    the rows as they were read.
    """
    event_id = add_published(session_factory, tenant, published_at=NOW - timedelta(days=30))

    report = pruner(session_factory, settings).run_once()

    assert isinstance(report, PruneReport)
    assert report.disabled is False
    assert report.truncated is False
    assert [(event.id, event.status, event.event_type) for event in report.pruned] == [
        (event_id, OutboxStatus.PUBLISHED.value, "contract.ingested")
    ]


# --- a full page is a floor -----------------------------------------------
def test_a_full_page_is_read_again_until_a_page_comes_back_short(
    session_factory: sessionmaker[Session], settings: Settings, tenant: Tenant
) -> None:
    """Three rows and a batch of one: a single-page pass would leave two behind.

    The command's one page per invocation is right for a person at a terminal and
    wrong for a timer — a 50 000-row backlog would take fifty days to drain. This is
    the test that fails if the loop is ever replaced by one call.
    """
    ids = [
        add_published(session_factory, tenant, published_at=NOW - timedelta(days=30 + offset))
        for offset in range(3)
    ]

    report = pruner(session_factory, settings, batch_size=1).run_once()

    assert report.count == 3
    assert sorted(event.id for event in report.pruned) == ids
    assert report.truncated is False
    assert remaining(session_factory) == []


def test_the_page_bound_is_admitted_rather_than_read_as_a_total(
    session_factory: sessionmaker[Session],
    settings: Settings,
    tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A truncated pass says so, for the reason a truncated field scan does.

    Without the flag, ``count`` on a bounded pass reads as "this is the backlog"
    when it is a floor — the reading that misleads an operator into believing the
    table is nearly clean. The remaining row is the proof the bound stopped it.
    """
    monkeypatch.setattr("app.workers.outbox_pruner.MAX_PRUNE_PAGES", 2)
    ids = [
        add_published(session_factory, tenant, published_at=NOW - timedelta(days=30 + offset))
        for offset in range(3)
    ]

    report = pruner(session_factory, settings, batch_size=1).run_once()

    assert report.count == 2
    assert report.truncated is True
    assert remaining(session_factory) == [ids[2]]


def test_a_second_pass_finds_nothing_and_says_so(
    session_factory: sessionmaker[Session], settings: Settings, tenant: Tenant
) -> None:
    """Pruning is idempotent, and the empty pass is still logged.

    A pass that found nothing is the normal state after the first one, so it must be
    cheap: no page loop, one line, and no exception.
    """
    add_published(session_factory, tenant, published_at=NOW - timedelta(days=30))
    worker = pruner(session_factory, settings)
    worker.run_once()

    with capture_logs() as captured:
        report = worker.run_once()

    assert report.pruned == ()
    assert events(captured) == ["outbox_prune_nothing_settled"]


# --- the log line ---------------------------------------------------------
def test_a_pass_that_deleted_reports_how_much_and_where(
    session_factory: sessionmaker[Session], settings: Settings, tenant: Tenant
) -> None:
    """The line carries the cut-off and the count, and no payload.

    Same event name as the command so one log query finds both paths to the same
    deletion. The payload is deliberately absent: an outbox body can quote contract
    detail, and this line is copied, shipped and retained.
    """
    add_published(session_factory, tenant, published_at=NOW - timedelta(days=30))

    with capture_logs() as captured:
        pruner(session_factory, settings).run_once()

    assert events(captured) == ["outbox_prune"]
    assert captured[0]["pruned"] == 1
    assert captured[0]["before"] == BEFORE.isoformat()
    assert captured[0]["retention_days"] == RETENTION
    assert captured[0]["truncated"] is False


# --- the loop -------------------------------------------------------------
def test_retention_off_is_announced_once_at_startup_and_never_per_tick(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    """One line, before the loop body runs, then silence for ever.

    Stopping before the first tick is the point: with retention off this process has
    nothing to do, so a per-tick line would be a daily "nothing happened" until the
    deployment ended — while the startup line still tells an operator why the
    container is running at all.
    """
    import threading

    stop = threading.Event()
    stop.set()

    with capture_logs() as captured:
        pruner(session_factory, settings, retention_days=0).run_forever(stop)

    assert events(captured) == ["outbox_prune_disabled"]


def test_an_enabled_pruner_announces_its_window_at_startup(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    """The enabled branch says its window, so a wrong ``.env`` is visible at boot."""
    import threading

    stop = threading.Event()
    stop.set()

    with capture_logs() as captured:
        pruner(session_factory, settings).run_forever(stop)

    assert events(captured) == ["outbox_pruner_started"]
    assert captured[0]["retention_days"] == RETENTION


def test_a_failed_pass_is_logged_and_the_loop_survives(
    session_factory: sessionmaker[Session],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database blip must not end the pruning for the deployment's life.

    ``restart: unless-stopped`` would bring a crashed container back, but a crash
    loop buries the line that says what is wrong — the same reasoning that keeps
    ``AttentionWatch`` from exiting non-zero on a finding. Each page is its own
    transaction, so a failed tick leaves nothing half-done.
    """
    import threading

    worker = pruner(session_factory, settings)
    stop = threading.Event()

    def _boom() -> None:
        stop.set()  # so the loop ends after this one failed tick
        raise RuntimeError("database went away")

    monkeypatch.setattr(worker, "run_once", _boom)

    with capture_logs() as captured:
        worker.run_forever(stop)  # returns normally; does not raise

    assert "outbox_prune_failed" in events(captured)
