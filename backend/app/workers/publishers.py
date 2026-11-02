"""Event transports for the outbox relay (F4).

The relay depends only on the
:class:`~app.workers.outbox_relay.EventPublisher` protocol. This module supplies
the concrete transport selected by ``EVENT_TRANSPORT`` and owns the wire format
of the event envelope, so the relay stays transport-agnostic (DECISIONS.md
D2-04).

Publishing is **synchronous and acknowledged**: :meth:`KafkaPublisher.publish`
returns only after the broker has acknowledged the record. That is what makes
the relay's rule — "returned normally" means "delivered" — actually true. A
fire-and-forget send would convert a broker outage into silent message loss,
which is the log-and-drop failure mode the specification forbids.

``kafka-python`` is imported lazily. A deployment with ``EVENT_TRANSPORT=none``
and every test that never reaches a broker therefore never imports it.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, cast

from app.core.config import Settings
from app.db.base import as_utc
from app.db.models.outbox import OutboxEvent
from app.workers.outbox_relay import (
    EventPublisher,
    UnavailablePublisher,
)

__all__ = [
    "KafkaPublisher",
    "Producer",
    "build_publisher",
    "create_kafka_producer",
    "encode_envelope",
    "encode_headers",
]


def encode_envelope(event: OutboxEvent) -> bytes:
    """Serialise one event as the message body.

    The identity fields travel *inside* the body as well as in the headers: a
    consumer that reads only the value must still be able to de-duplicate on
    ``event_id`` and scope its work to ``tenant_id`` before parsing the payload.

    ``default=str`` renders the naive-UTC timestamps and any UUID inside the
    payload deterministically instead of raising mid-publish. Keys are sorted so
    the same event always produces byte-identical output, which keeps consumer
    deduplication and tests stable.
    """
    occurred_at = as_utc(event.created_at) or event.created_at
    envelope: dict[str, Any] = {
        "correlation_id": event.correlation_id,
        "event_id": str(event.event_id),
        "event_type": event.event_type,
        "occurred_at": occurred_at.isoformat(),
        "payload": event.payload,
        "schema_version": event.schema_version,
        "tenant_id": str(event.tenant_id),
    }
    return json.dumps(envelope, separators=(",", ":"), sort_keys=True, default=str).encode("utf-8")


def encode_headers(event: OutboxEvent) -> list[tuple[str, bytes]]:
    """Kafka record headers — the routing/dedup fields without the payload.

    Kafka headers must be bytes, so each value is encoded here rather than
    relying on a serializer being installed on the producer.
    """
    return [
        ("correlation_id", event.correlation_id.encode("utf-8")),
        ("event_id", str(event.event_id).encode("utf-8")),
        ("event_type", event.event_type.encode("utf-8")),
        ("schema_version", str(event.schema_version).encode("utf-8")),
        ("tenant_id", str(event.tenant_id).encode("utf-8")),
    ]


class _AckedRecord(Protocol):
    """The part of a produced record's future this module needs."""

    def get(self, timeout: float | None = ...) -> Any: ...


class Producer(Protocol):
    """The slice of ``kafka.KafkaProducer`` the publisher uses.

    Narrowed to a protocol so tests exercise the real publish path with a fake
    producer instead of needing a broker.
    """

    def send(
        self,
        topic: str,
        value: bytes | None = ...,
        key: bytes | None = ...,
        headers: list[tuple[str, bytes]] | None = ...,
    ) -> _AckedRecord: ...


class KafkaPublisher:
    """Publishes outbox events to Kafka, waiting for broker acknowledgement."""

    def __init__(self, producer: Producer, *, ack_timeout_seconds: float) -> None:
        self._producer = producer
        self._ack_timeout = ack_timeout_seconds

    def publish(self, event: OutboxEvent) -> None:
        """Send one event and block until the broker acknowledges it.

        Errors are deliberately **not** caught: the relay treats a raised
        exception as a failed publish, records the error against the row and
        retries with backoff. Swallowing one here would mark an undelivered
        event as published.
        """
        record = self._producer.send(
            event.topic,
            value=encode_envelope(event),
            key=event.partition_key.encode("utf-8"),
            headers=encode_headers(event),
        )
        record.get(timeout=self._ack_timeout)


def create_kafka_producer(settings: Settings) -> Producer:
    """Build the real Kafka producer from settings.

    ``acks="all"`` makes an acknowledgement mean "all in-sync replicas have it",
    so a leader crash immediately after a publish cannot lose the event.
    ``linger_ms=0`` disables batching: the relay publishes one row at a time and
    already waits for each ack, so batching would only add latency.
    """
    from kafka import KafkaProducer  # local import: keeps the dep optional

    producer = KafkaProducer(
        bootstrap_servers=settings.kafka_servers,
        client_id=settings.kafka_client_id,
        acks="all",
        retries=settings.kafka_produce_retries,
        linger_ms=0,
    )
    # `kafka` ships no type information, so mypy sees the constructor as Any.
    return cast(Producer, producer)


def build_publisher(settings: Settings) -> EventPublisher:
    """Select the transport named by ``EVENT_TRANSPORT``.

    The two-branch body is total over ``EventTransport``; an unknown value
    cannot reach here because settings validation rejects it at start-up.
    """
    if settings.event_transport == "kafka":
        return KafkaPublisher(
            create_kafka_producer(settings),
            ack_timeout_seconds=settings.kafka_publish_timeout_seconds,
        )
    return UnavailablePublisher(reason=f"EVENT_TRANSPORT={settings.event_transport}")
