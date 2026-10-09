"""Async Kafka consumer base — aiokafka."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from aiokafka import AIOKafkaConsumer

from contract_agent.config import Settings
from contract_agent.logging_config import get_logger

logger = get_logger(__name__)

# Handler receives a fully-decoded dict payload.
Handler = Callable[[dict[str, Any]], Awaitable[None]]


def _json_deserializer(raw: bytes | None) -> dict[str, Any]:
    """Decode a Kafka message value into a dict."""
    if raw is None:
        return {}
    decoded = json.loads(raw.decode("utf-8"))
    if not isinstance(decoded, dict):
        return {"_raw": decoded}
    return decoded


class KafkaConsumer:
    """Base async consumer. Subclass or pass handler in M18."""

    # NOTE: AIOKafkaConsumer is NOT generic — do not add [str, dict] here.
    _consumer: AIOKafkaConsumer | None

    def __init__(
        self,
        settings: Settings,
        *,
        topic: str,
        group_id: str | None = None,
        handler: Handler | None = None,
    ) -> None:
        self._settings = settings
        self._topic = topic
        self._group_id = group_id or settings.kafka_consumer_group
        self._handler = handler
        self._consumer = None
        self._enabled = settings.kafka_enabled
        self._running = False

    async def start(self) -> None:
        if not self._enabled:
            logger.info(
                "kafka_consumer_disabled",
                topic=self._topic,
                reason="KAFKA_ENABLED=false",
            )
            return
        try:
            self._consumer = AIOKafkaConsumer(
                self._topic,
                bootstrap_servers=self._settings.kafka_bootstrap_servers,
                group_id=self._group_id,
                client_id=self._settings.kafka_client_id,
                value_deserializer=_json_deserializer,
                auto_offset_reset="earliest",
                enable_auto_commit=True,
            )
            await self._consumer.start()
            self._running = True
            logger.info(
                "kafka_consumer_started",
                topic=self._topic,
                group=self._group_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "kafka_consumer_start_failed",
                topic=self._topic,
                error=str(exc),
            )
            self._consumer = None

    async def stop(self) -> None:
        self._running = False
        if self._consumer is not None:
            await self._consumer.stop()
            logger.info("kafka_consumer_stopped", topic=self._topic)

    async def run(self) -> None:
        """Consume loop — calls handler for each message."""
        if self._consumer is None or self._handler is None:
            return
        try:
            async for msg in self._consumer:
                if not self._running:
                    break

                # Defensive narrowing: deserializer returns dict, but tombstones
                # or malformed JSON from external producers can still surprise us.
                value: Any = msg.value
                if not isinstance(value, dict):
                    logger.warning(
                        "kafka_consumer_unexpected_value_type",
                        topic=self._topic,
                        offset=msg.offset,
                        value_type=type(value).__name__,
                    )
                    continue

                try:
                    await self._handler(value)
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "kafka_handler_failed",
                        topic=self._topic,
                        error=str(exc),
                        offset=msg.offset,
                    )
                    # M18: route to DLQ here
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "kafka_consumer_loop_failed",
                topic=self._topic,
                error=str(exc),
            )
