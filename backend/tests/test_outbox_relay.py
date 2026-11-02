"""Outbox relay behaviour, including the "no transport yet" path.

Between F2 and F4 there is no event bus, so the relay is wired to
``UnavailablePublisher``. These tests pin down that this degrades safely: the
event is retried and later dead-lettered, and is never marked published.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy.orm import Session, sessionmaker

from app.db.base import utc_now
from app.db.enums import OutboxStatus
from app.db.models import OutboxEvent
from app.db.session import session_scope
from app.workers.outbox_relay import (
    OutboxRelay,
    PublisherUnavailableError,
    UnavailablePublisher,
)

MAX_ATTEMPTS = 3


class RecordingPublisher:
    """Test double that records what the relay handed it."""

    def __init__(self) -> None:
        self.published: list[int] = []

    def publish(self, event: OutboxEvent) -> None:
        self.published.append(event.id)


class FailingPublisher:
    def __init__(self, message: str = "broker unreachable") -> None:
        self.message = message
        self.calls = 0

    def publish(self, event: OutboxEvent) -> None:
        self.calls += 1
        raise RuntimeError(self.message)


def make_relay(
    session_factory: sessionmaker[Session],
    publisher: object,
    **overrides: object,
) -> OutboxRelay:
    options: dict[str, object] = {
        "batch_size": 10,
        "lease_seconds": 30,
        "max_attempts": MAX_ATTEMPTS,
        "poll_interval_seconds": 0.01,
        "worker_id": "relay-test",
    }
    options.update(overrides)
    return OutboxRelay(session_factory, publisher, **options)  # type: ignore[arg-type]


def load(session_factory: sessionmaker[Session], event_id: int) -> OutboxEvent:
    with session_scope(session_factory) as session:
        row = session.get(OutboxEvent, event_id)
    assert row is not None
    return row


def test_run_once_publishes_and_marks_published(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    event_id = enqueue_event()
    publisher = RecordingPublisher()

    published = make_relay(session_factory, publisher).run_once()

    assert published == 1
    assert publisher.published == [event_id]
    assert load(session_factory, event_id).status is OutboxStatus.PUBLISHED


def test_run_once_is_a_no_op_when_nothing_is_due(
    session_factory: sessionmaker[Session],
) -> None:
    publisher = RecordingPublisher()

    assert make_relay(session_factory, publisher).run_once() == 0
    assert publisher.published == []


def test_two_relays_never_publish_the_same_row(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    enqueue_event()
    first, second = RecordingPublisher(), RecordingPublisher()

    total = make_relay(session_factory, first, worker_id="relay-1").run_once()
    total += make_relay(session_factory, second, worker_id="relay-2").run_once()

    assert total == 1
    assert len(first.published) + len(second.published) == 1


def test_a_failed_publish_returns_the_row_to_pending(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    event_id = enqueue_event()
    publisher = FailingPublisher()

    published = make_relay(session_factory, publisher).run_once()

    row = load(session_factory, event_id)
    assert published == 0
    assert publisher.calls == 1
    assert row.status is OutboxStatus.PENDING
    assert row.attempts == 1
    assert row.locked_by is None
    assert "broker unreachable" in (row.last_error or "")


def test_the_provisional_publisher_never_reports_success(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    event_id = enqueue_event()

    published = make_relay(session_factory, UnavailablePublisher()).run_once()

    row = load(session_factory, event_id)
    assert published == 0
    # Still pending, with the real reason recorded — not silently discarded.
    assert row.status is OutboxStatus.PENDING
    assert row.published_at is None
    assert row.last_error is not None
    assert PublisherUnavailableError.__name__ in row.last_error


def test_repeated_failures_end_in_the_failed_state(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    event_id = enqueue_event()
    relay = make_relay(session_factory, UnavailablePublisher())

    for _ in range(MAX_ATTEMPTS):
        relay.run_once()
        with session_scope(session_factory) as session:
            row = session.get(OutboxEvent, event_id)
            assert row is not None
            # Force the backoff timer to elapse so the next tick picks it up
            # without the test having to sleep for real.
            row.next_attempt_at = utc_now() - timedelta(seconds=1)

    assert load(session_factory, event_id).status is OutboxStatus.FAILED


def test_batch_size_bounds_one_tick(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    for _ in range(4):
        enqueue_event()
    publisher = RecordingPublisher()

    published = make_relay(session_factory, publisher, batch_size=2).run_once()

    assert published == 2
    assert len(publisher.published) == 2


def test_run_forever_exits_cleanly_when_stopped(
    session_factory: sessionmaker[Session],
) -> None:
    stop = threading.Event()
    stop.set()

    relay = make_relay(session_factory, RecordingPublisher())
    relay.run_forever(stop)  # must return promptly, not hang


def test_run_forever_processes_rows_then_stops(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    enqueue_event()
    publisher = RecordingPublisher()
    relay = make_relay(session_factory, publisher)
    stop = threading.Event()

    thread = threading.Thread(target=relay.run_forever, args=(stop,), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5.0
        while not publisher.published and time.monotonic() < deadline:
            time.sleep(0.01)
        assert publisher.published, "relay never published the pending row"
    finally:
        stop.set()
        thread.join(timeout=5.0)

    assert not thread.is_alive(), "relay did not honour the stop signal"
