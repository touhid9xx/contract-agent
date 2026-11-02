"""Dead-letter inspection and replay (F5).

The relay parks a poison row so a human can decide later — that was F3. "Later"
needed a tool and the tool needs rules, which are what this file pins down:

* replay touches **only** parked rows. Resurrecting a published event would send
  a duplicate the business write never asked for, and touching a pending or
  publishing row would collide with a live relay;
* the attempt counter resets, because the budget exists to stop a retry loop, not
  to cap an operator's total number of recoveries;
* the reason the row was parked survives the reset, in the operator's log.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from app.cli import EXIT_NO_MATCH, EXIT_OK
from app.cli import main as cli_main
from app.core.config import Settings
from app.db.base import utc_now
from app.db.enums import OutboxStatus
from app.db.models import OutboxEvent
from app.db.repositories import (
    OutboxRepository,
    dead_lettered,
    replay_dead_lettered,
)
from app.db.session import session_scope

LEASE_SECONDS = 30
MAX_ATTEMPTS = 3
WORKER = "relay-a"
PARK_ERROR = "RuntimeError: NoBrokersAvailable"


def relay(session: Session) -> OutboxRepository:
    return OutboxRepository(
        session,
        worker_id=WORKER,
        lease_seconds=LEASE_SECONDS,
        max_attempts=MAX_ATTEMPTS,
    )


def park(
    session_factory: sessionmaker[Session],
    event_id: int,
    *,
    attempts: int = MAX_ATTEMPTS,
    error: str = PARK_ERROR,
) -> None:
    """Put a row in the dead-letter state, as the relay would after a poison run."""
    with session_scope(session_factory) as session:
        session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(
                status=OutboxStatus.FAILED,
                attempts=attempts,
                last_error=error,
                locked_by=None,
                locked_until=None,
            )
            .execution_options(synchronize_session=False)
        )


def load(session_factory: sessionmaker[Session], event_id: int) -> OutboxEvent:
    with session_scope(session_factory) as session:
        row = session.get(OutboxEvent, event_id)
        assert row is not None
        return row


# --- replay ---------------------------------------------------------------


def test_a_parked_row_is_requeued_with_a_fresh_budget(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    event_id = enqueue_event()
    park(session_factory, event_id)

    with session_scope(session_factory) as session:
        replays = replay_dead_lettered(session)

    row = load(session_factory, event_id)
    assert [replay.id for replay in replays] == [event_id]
    assert row.status is OutboxStatus.PENDING
    assert row.attempts == 0
    assert row.last_error is None
    assert row.locked_by is None
    assert row.locked_until is None
    assert row.next_attempt_at <= utc_now()


def test_replay_returns_the_state_it_replaced(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    """The row no longer carries its error, so the caller has to be given it."""
    event_id = enqueue_event()
    park(session_factory, event_id)

    with session_scope(session_factory) as session:
        (replay,) = replay_dead_lettered(session)

    assert replay.id == event_id
    assert replay.event_type == "contract.ingested"
    assert replay.attempts == MAX_ATTEMPTS
    assert replay.last_error == PARK_ERROR
    assert replay.tenant_id


def test_the_requeued_time_is_floored_to_stored_precision(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    """D2-06: MySQL rounds sub-second DATETIME, which could land in the future."""
    event_id = enqueue_event()
    park(session_factory, event_id)

    with session_scope(session_factory) as session:
        replay_dead_lettered(session)

    assert load(session_factory, event_id).next_attempt_at.microsecond == 0


def test_replay_never_resurrects_a_published_row(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    event_id = enqueue_event()
    with session_scope(session_factory) as session:
        session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(
                status=OutboxStatus.PUBLISHED,
                attempts=1,
                published_at=utc_now(),
            )
            .execution_options(synchronize_session=False)
        )

    with session_scope(session_factory) as session:
        replays = replay_dead_lettered(session)

    assert replays == []
    assert load(session_factory, event_id).status is OutboxStatus.PUBLISHED


def test_replay_leaves_a_pending_row_alone(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    """A row already queued is the relay's business, not an operator's."""
    event_id = enqueue_event()

    with session_scope(session_factory) as session:
        assert replay_dead_lettered(session) == []

    row = load(session_factory, event_id)
    assert row.status is OutboxStatus.PENDING
    assert row.attempts == 0


def test_replay_respects_the_limit(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    ids = [enqueue_event() for _ in range(3)]
    for event_id in ids:
        park(session_factory, event_id)

    with session_scope(session_factory) as session:
        replays = replay_dead_lettered(session, limit=2)

    assert [replay.id for replay in replays] == ids[:2]
    assert load(session_factory, ids[2]).status is OutboxStatus.FAILED


def test_replay_can_target_specific_ids(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    ids = [enqueue_event() for _ in range(3)]
    for event_id in ids:
        park(session_factory, event_id)

    with session_scope(session_factory) as session:
        replays = replay_dead_lettered(session, event_ids=[ids[1]])

    assert [replay.id for replay in replays] == [ids[1]]
    assert load(session_factory, ids[0]).status is OutboxStatus.FAILED
    assert load(session_factory, ids[2]).status is OutboxStatus.FAILED


def test_a_dry_run_reports_without_changing_anything(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    event_id = enqueue_event()
    park(session_factory, event_id)

    with session_scope(session_factory) as session:
        replays = replay_dead_lettered(session, dry_run=True)

    row = load(session_factory, event_id)
    assert [replay.id for replay in replays] == [event_id]
    assert row.status is OutboxStatus.FAILED
    assert row.attempts == MAX_ATTEMPTS
    assert row.last_error == PARK_ERROR


def test_a_replayed_row_is_claimable_by_the_relay_again(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    """The point of the whole exercise: delivery resumes, with a full budget."""
    event_id = enqueue_event()
    park(session_factory, event_id)

    with session_scope(session_factory) as session:
        replay_dead_lettered(session)

    with session_scope(session_factory) as session:
        claimed = relay(session).claim_due(10)

    assert [event.id for event in claimed] == [event_id]
    assert claimed[0].status is OutboxStatus.PUBLISHING
    assert claimed[0].attempts == 1


def test_dead_lettered_lists_only_parked_rows_oldest_first(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    parked_ids = [enqueue_event(), enqueue_event()]
    for event_id in parked_ids:
        park(session_factory, event_id)
    enqueue_event()  # pending: must not appear

    with session_scope(session_factory) as session:
        rows = dead_lettered(session)

    assert [row.id for row in rows] == parked_ids
    assert rows[0].last_error == PARK_ERROR


# --- the operator command -------------------------------------------------


def test_the_command_lists_parked_rows_without_changing_them(
    session_factory: sessionmaker[Session],
    settings: Settings,
    enqueue_event: Callable[..., int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    event_id = enqueue_event()
    park(session_factory, event_id)

    code = cli_main(["outbox-replay", "--list"], settings=settings, session_factory=session_factory)

    assert code == EXIT_OK
    assert load(session_factory, event_id).status is OutboxStatus.FAILED
    # The parked reason is what an operator came for, so it has to be printed.
    captured = capsys.readouterr()
    assert PARK_ERROR in captured.out + captured.err


def test_the_command_requeues_and_reports_the_previous_error(
    session_factory: sessionmaker[Session],
    settings: Settings,
    enqueue_event: Callable[..., int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    event_id = enqueue_event()
    park(session_factory, event_id)

    code = cli_main(["outbox-replay"], settings=settings, session_factory=session_factory)

    assert code == EXIT_OK
    assert load(session_factory, event_id).status is OutboxStatus.PENDING
    captured = capsys.readouterr()
    assert PARK_ERROR in captured.out + captured.err


def test_the_command_dry_run_writes_nothing(
    session_factory: sessionmaker[Session],
    settings: Settings,
    enqueue_event: Callable[..., int],
) -> None:
    event_id = enqueue_event()
    park(session_factory, event_id)

    code = cli_main(
        ["outbox-replay", "--dry-run"], settings=settings, session_factory=session_factory
    )

    assert code == EXIT_OK
    assert load(session_factory, event_id).status is OutboxStatus.FAILED


def test_the_command_fails_loudly_when_an_id_is_not_parked(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    """A wrong id usually means the operator is looking at another database."""
    code = cli_main(
        ["outbox-replay", "--ids", "999"], settings=settings, session_factory=session_factory
    )

    assert code == EXIT_NO_MATCH


def test_the_command_is_a_no_op_when_nothing_is_parked(
    session_factory: sessionmaker[Session],
    settings: Settings,
    enqueue_event: Callable[..., int],
) -> None:
    """Steady state must not look like a failure to a periodic run."""
    enqueue_event()

    code = cli_main(["outbox-replay"], settings=settings, session_factory=session_factory)

    assert code == EXIT_OK
