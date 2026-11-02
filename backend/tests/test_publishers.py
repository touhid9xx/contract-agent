"""Tests for the F4 event transports.

Everything here runs without a broker: :class:`KafkaPublisher` talks to an
injected producer (a Protocol), so the real publish path — envelope encoding,
headers, acknowledgement, failure propagation — is exercised for real. The
``live`` test at the bottom is opt-in via ``KAFKA_LIVE=1``.

The central property under test is the relay's contract: **"returned normally"
means "delivered"**. A transport that reported success without a broker
acknowledgement would let the outbox mark an undelivered event as published,
which is the log-and-drop failure the specification forbids.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.enums import OutboxStatus
from app.db.models.outbox import OutboxEvent
from app.db.session import session_scope
from app.workers import publishers
from app.workers.outbox_relay import (
    OutboxRelay,
    PublisherUnavailableError,
    UnavailablePublisher,
)
from app.workers.publishers import (
    KafkaPublisher,
    build_publisher,
    encode_envelope,
    encode_headers,
)

ACK_TIMEOUT = 2.5


class FakeRecord:
    """Stands in for Kafka's ``FutureRecordMetadata``."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.get_timeout: float | None = None

    def get(self, timeout: float | None = None) -> Any:
        self.get_timeout = timeout
        if self.error is not None:
            raise self.error
        return SimpleNamespace(topic="contract.ingested", partition=0, offset=7)


class FakeProducer:
    """Records every ``send`` and hands back a controllable ack future."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.sent: list[dict[str, Any]] = []
        self.records: list[FakeRecord] = []

    def send(
        self,
        topic: str,
        value: bytes | None = None,
        key: bytes | None = None,
        headers: list[tuple[str, bytes]] | None = None,
    ) -> FakeRecord:
        self.sent.append({"topic": topic, "value": value, "key": key, "headers": headers})
        record = FakeRecord(self.error)
        self.records.append(record)
        return record


def make_publisher(producer: FakeProducer) -> KafkaPublisher:
    return KafkaPublisher(producer, ack_timeout_seconds=ACK_TIMEOUT)


def load(session_factory: sessionmaker[Session], event_id: int) -> OutboxEvent:
    with session_scope(session_factory) as session:
        row = session.get(OutboxEvent, event_id)
    assert row is not None
    return row


def make_relay(session_factory: sessionmaker[Session], publisher: object) -> OutboxRelay:
    return OutboxRelay(
        session_factory,
        publisher,  # type: ignore[arg-type]
        batch_size=10,
        lease_seconds=30,
        max_attempts=3,
        poll_interval_seconds=0.01,
        worker_id="relay-test",
    )


# --------------------------------------------------------------------- encoding
def test_envelope_carries_identity_next_to_the_payload(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    event_id = enqueue_event()
    row = load(session_factory, event_id)

    body = json.loads(encode_envelope(row))

    assert body["event_id"] == str(row.event_id)
    assert body["tenant_id"] == str(row.tenant_id)
    assert body["correlation_id"] == row.correlation_id
    assert body["event_type"] == "contract.ingested"
    assert body["schema_version"] == 1
    assert body["payload"] == {"contract_id": 1042}


def test_envelope_timestamp_is_utc_aware(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    """Stored timestamps are naive UTC (D2-01); the wire form must not lose that."""
    event_id = enqueue_event()
    row = load(session_factory, event_id)

    occurred_at = json.loads(encode_envelope(row))["occurred_at"]

    assert occurred_at.endswith("+00:00")


def test_envelope_encoding_is_deterministic(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    """Identical rows must produce identical bytes, or dedup and tests drift."""
    row = load(session_factory, enqueue_event())

    assert encode_envelope(row) == encode_envelope(row)
    assert b" " not in encode_envelope(row)


def test_headers_are_bytes_and_omit_the_payload(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    row = load(session_factory, enqueue_event())

    headers = dict(encode_headers(row))

    assert headers["event_id"] == str(row.event_id).encode()
    assert headers["tenant_id"] == str(row.tenant_id).encode()
    assert headers["event_type"] == b"contract.ingested"
    assert headers["schema_version"] == b"1"
    assert headers["correlation_id"] == row.correlation_id.encode()
    assert all(
        isinstance(name, str) and isinstance(value, bytes) for name, value in headers.items()
    )


# -------------------------------------------------------------------- publishing
def test_publish_sends_to_the_event_topic_with_the_partition_key(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    row = load(session_factory, enqueue_event())
    producer = FakeProducer()

    make_publisher(producer).publish(row)

    assert len(producer.sent) == 1
    sent = producer.sent[0]
    assert sent["topic"] == "contract.ingested"
    # Partitioning by contract id keeps one contract's events ordered.
    assert sent["key"] == b"1042"
    assert json.loads(sent["value"])["event_id"] == str(row.event_id)
    assert dict(sent["headers"])["event_id"] == str(row.event_id).encode()


def test_publish_waits_for_acknowledgement(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    """The ack wait is what makes "returned normally" equal "delivered"."""
    row = load(session_factory, enqueue_event())
    producer = FakeProducer()

    make_publisher(producer).publish(row)

    assert producer.records[0].get_timeout == ACK_TIMEOUT


def test_publish_propagates_a_broker_error(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    """A swallowed error here would let the relay mark a lost event published."""
    row = load(session_factory, enqueue_event())
    producer = FakeProducer(error=RuntimeError("broker unreachable"))

    with pytest.raises(RuntimeError, match="broker unreachable"):
        make_publisher(producer).publish(row)


# ---------------------------------------------------------------------- factory
def test_the_default_transport_refuses_every_publish() -> None:
    publisher = build_publisher(Settings())

    assert isinstance(publisher, UnavailablePublisher)
    with pytest.raises(PublisherUnavailableError):
        publisher.publish(SimpleNamespace(event_id="x"))  # type: ignore[arg-type]


def test_the_none_transport_names_itself_in_the_error() -> None:
    publisher = build_publisher(Settings(event_transport="none"))

    with pytest.raises(PublisherUnavailableError, match="EVENT_TRANSPORT=none"):
        publisher.publish(SimpleNamespace(event_id="x"))  # type: ignore[arg-type]


def test_the_kafka_transport_builds_a_kafka_publisher(
    session_factory: sessionmaker[Session],
    enqueue_event: Callable[..., int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = FakeProducer()

    def _no_broker(_settings: Settings) -> FakeProducer:
        """Stands in for the real producer factory: never dials a broker."""
        return producer

    monkeypatch.setattr(publishers, "create_kafka_producer", _no_broker)

    publisher = build_publisher(Settings(event_transport="kafka"))

    assert isinstance(publisher, KafkaPublisher)
    # Prove the factory wired *this* producer through, rather than only that the
    # right class came back.
    publisher.publish(load(session_factory, enqueue_event()))
    assert len(producer.sent) == 1


def test_settings_reject_kafka_without_bootstrap_servers() -> None:
    with pytest.raises(ValidationError, match="must list at least one host"):
        Settings(event_transport="kafka", kafka_bootstrap_servers="  ,  ")


def test_settings_reject_a_publish_timeout_that_outlives_the_lease() -> None:
    """Otherwise a slow broker lets the lease expire and the row is published twice."""
    with pytest.raises(ValidationError, match="below OUTBOX_LEASE_SECONDS"):
        Settings(
            event_transport="kafka",
            kafka_publish_timeout_seconds=120,
            outbox_lease_seconds=120,
        )


# ------------------------------------------------------------------ relay wiring
def test_the_relay_marks_a_kafka_acknowledged_event_published(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    event_id = enqueue_event()
    producer = FakeProducer()

    published = make_relay(session_factory, make_publisher(producer)).run_once()

    assert published == 1
    assert load(session_factory, event_id).status is OutboxStatus.PUBLISHED
    assert len(producer.sent) == 1


def test_a_broker_outage_leaves_the_row_pending_not_published(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    event_id = enqueue_event()
    producer = FakeProducer(error=RuntimeError("broker unreachable"))

    published = make_relay(session_factory, make_publisher(producer)).run_once()

    row = load(session_factory, event_id)
    assert published == 0
    assert row.status is OutboxStatus.PENDING
    assert row.published_at is None
    assert "broker unreachable" in (row.last_error or "")


# ------------------------------------------------------------------- live (opt-in)
@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("KAFKA_LIVE") != "1",
    reason="set KAFKA_LIVE=1 with a reachable broker to run this",
)
def test_a_real_kafka_broker_acknowledges_the_event(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    """Opt-in: needs a real broker. Proves the ack path against a real Kafka peer."""
    settings = Settings(event_transport="kafka")
    row = load(session_factory, enqueue_event())

    publisher = KafkaPublisher(
        publishers.create_kafka_producer(settings),
        ack_timeout_seconds=settings.kafka_publish_timeout_seconds,
    )
    # Returns only once the broker has acknowledged it; a silent no-op producer
    # cannot satisfy this.
    publisher.publish(row)
