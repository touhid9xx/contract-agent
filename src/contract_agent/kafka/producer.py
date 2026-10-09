"""Async Kafka producer — aiokafka, JSON serialization, graceful degradation.

Why aiokafka?
    - Native async — fits FastAPI event loop
    - Same client works against StrikeMQ (local) and Kafka (prod)

Why graceful degradation?
    - Kafka down → API must still respond
    - Events buffered in Redis (M18) or dropped with warning
"""

from __future__ import annotations

import json
from typing import Any

from aiokafka import AIOKafkaProducer

from contract_agent.config import Settings
from contract_agent.kafka.events import BaseEvent
from contract_agent.logging_config import get_logger

logger = get_logger(__name__)


class KafkaProducer:
    """Thin async wrapper around AIOKafkaProducer."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._producer: AIOKafkaProducer | None = None
        self._enabled = settings.kafka_enabled

    async def start(self) -> None:
        if not self._enabled:
            logger.info("kafka_producer_disabled", reason="KAFKA_ENABLED=false")
            return
        try:
            self._producer = AIOKafkaProducer(
                bootstrap_servers=self._settings.kafka_bootstrap_servers,
                client_id=self._settings.kafka_client_id,
                value_serializer=lambda v: json.dumps(v, default=str).encode("utf-8"),
                key_serializer=lambda k: k.encode("utf-8") if k else None,
                acks="all",
                enable_idempotence=True,
                request_timeout_ms=10_000,
            )
            await self._producer.start()
            logger.info("kafka_producer_started", servers=self._settings.kafka_bootstrap_servers)
        except Exception as exc:  # noqa: BLE001
            logger.error("kafka_producer_start_failed", error=str(exc))
            self._producer = None

    async def stop(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            logger.info("kafka_producer_stopped")

    async def publish(
        self,
        topic: str,
        event: BaseEvent | dict[str, Any],
        *,
        key: str | None = None,
    ) -> bool:
        """Publish an event. Returns True on success, False if disabled/failed."""
        if not self._enabled or self._producer is None:
            logger.debug("kafka_publish_skipped", topic=topic, reason="disabled_or_unavailable")
            return False

        payload = event.model_dump(mode="json") if isinstance(event, BaseEvent) else event
        try:
            await self._producer.send_and_wait(topic, value=payload, key=key)
            logger.info("kafka_published", topic=topic, event_type=payload.get("event_type"))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("kafka_publish_failed", topic=topic, error=str(exc))
            return False
