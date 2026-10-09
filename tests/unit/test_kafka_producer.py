"""Kafka producer tests — disabled mode, event serialization."""

from __future__ import annotations

from uuid import uuid4

import pytest

from contract_agent.kafka.events import ContractIngestedEvent
from contract_agent.kafka.producer import KafkaProducer


@pytest.mark.asyncio
async def test_producer_noop_when_disabled(settings) -> None:
    # settings.kafka_enabled is False in test env
    producer = KafkaProducer(settings)
    await producer.start()
    assert producer._producer is None
    ok = await producer.publish("any.topic", {"x": 1})
    assert ok is False
    await producer.stop()


@pytest.mark.asyncio
async def test_producer_publish_returns_false_if_not_started(settings) -> None:
    producer = KafkaProducer(settings)
    # Never call start()
    ok = await producer.publish("any.topic", {"x": 1})
    assert ok is False


def test_contract_ingested_event_serializes() -> None:
    tid = uuid4()
    event = ContractIngestedEvent(
        tenant_id=tid,
        correlation_id="req-123",
        payload={"contract_id": "c-1", "filename": "a.pdf"},
    )
    dumped = event.model_dump(mode="json")
    assert dumped["event_type"] == "contract.ingested"
    assert dumped["tenant_id"] == str(tid)
    assert dumped["payload"]["contract_id"] == "c-1"
    assert "event_id" in dumped
    assert "occurred_at" in dumped


def test_event_requires_tenant_id() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ContractIngestedEvent(payload={})  # type: ignore[call-arg]
