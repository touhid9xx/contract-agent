"""Kafka consumer tests — deep coverage of start/stop/run paths.

API surface (from src/contract_agent/kafka/consumer.py):
    - _json_deserializer(raw) -> dict
    - KafkaConsumer(settings, topic=..., group_id=..., handler=...)
    - .start() / .stop() / .run()

Coverage strategy:
    - Deserializer edge cases (None, dict, non-dict, unicode, malformed)
    - KAFKA_ENABLED=false → no-op start
    - KAFKA_ENABLED=true → mock AIOKafkaConsumer start/stop
    - run() loop with fake messages (dict / non-dict / handler raises)
    - Loop cancellation via _running=False
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from contract_agent.config import get_settings
from contract_agent.kafka.consumer import KafkaConsumer, _json_deserializer


# ============================================================
# FIXTURES
# ============================================================
@pytest.fixture()
def settings():
    return get_settings()


@pytest.fixture()
def enabled_settings(monkeypatch: pytest.MonkeyPatch):
    """Settings with KAFKA_ENABLED=true (mock broker still needed)."""
    monkeypatch.setenv("KAFKA_ENABLED", "true")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    yield get_settings()
    monkeypatch.delenv("KAFKA_ENABLED", raising=False)
    get_settings.cache_clear()  # type: ignore[attr-defined]


# ============================================================
# DESERIALIZER
# ============================================================
def test_deserializer_handles_none() -> None:
    assert _json_deserializer(None) == {}


def test_deserializer_handles_dict_json() -> None:
    raw = json.dumps({"event_type": "test", "n": 1}).encode()
    assert _json_deserializer(raw) == {"event_type": "test", "n": 1}


def test_deserializer_wraps_non_dict_json() -> None:
    raw = json.dumps([1, 2, 3]).encode()
    assert _json_deserializer(raw) == {"_raw": [1, 2, 3]}


def test_deserializer_wraps_scalar_json() -> None:
    raw = json.dumps("just a string").encode()
    assert _json_deserializer(raw) == {"_raw": "just a string"}


def test_deserializer_handles_unicode() -> None:
    raw = json.dumps({"name": "বাংলা"}).encode("utf-8")
    assert _json_deserializer(raw) == {"name": "বাংলা"}


def test_deserializer_raises_on_malformed_json() -> None:
    """Bad JSON → JSONDecodeError propagates."""
    with pytest.raises(json.JSONDecodeError):
        _json_deserializer(b"not json{")


# ============================================================
# LIFECYCLE — disabled (KAFKA_ENABLED=false)
# ============================================================
@pytest.mark.asyncio
async def test_start_disabled_noop(settings: Any) -> None:
    consumer = KafkaConsumer(settings, topic="t")
    await consumer.start()
    assert consumer._consumer is None
    assert consumer._running is False
    await consumer.stop()


@pytest.mark.asyncio
async def test_stop_without_start(settings: Any) -> None:
    consumer = KafkaConsumer(settings, topic="t")
    await consumer.stop()  # no error


# ============================================================
# LIFECYCLE — enabled (mock AIOKafkaConsumer)
# ============================================================
@pytest.mark.asyncio
async def test_start_enabled_creates_consumer(
    enabled_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KAFKA_ENABLED=true → AIOKafkaConsumer constructed + started."""
    mock_consumer = MagicMock()
    mock_consumer.start = AsyncMock()

    monkeypatch.setattr(
        "contract_agent.kafka.consumer.AIOKafkaConsumer",
        lambda *a, **kw: mock_consumer,
    )

    consumer = KafkaConsumer(enabled_settings, topic="t")
    await consumer.start()

    assert consumer._consumer is mock_consumer
    assert consumer._running is True
    mock_consumer.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_enabled_handles_exception(
    enabled_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AIOKafkaConsumer.start() raises → logged, _consumer=None, no crash."""
    mock_consumer = MagicMock()
    mock_consumer.start = AsyncMock(side_effect=RuntimeError("broker down"))

    monkeypatch.setattr(
        "contract_agent.kafka.consumer.AIOKafkaConsumer",
        lambda *a, **kw: mock_consumer,
    )

    consumer = KafkaConsumer(enabled_settings, topic="t")
    await consumer.start()  # should NOT raise

    assert consumer._consumer is None
    assert consumer._running is False


@pytest.mark.asyncio
async def test_stop_with_consumer_calls_stop(
    enabled_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stop() → calls underlying AIOKafkaConsumer.stop()."""
    mock_consumer = MagicMock()
    mock_consumer.start = AsyncMock()
    mock_consumer.stop = AsyncMock()

    monkeypatch.setattr(
        "contract_agent.kafka.consumer.AIOKafkaConsumer",
        lambda *a, **kw: mock_consumer,
    )

    consumer = KafkaConsumer(enabled_settings, topic="t")
    await consumer.start()
    await consumer.stop()

    mock_consumer.stop.assert_awaited_once()
    assert consumer._running is False


# ============================================================
# RUN LOOP — early returns
# ============================================================
@pytest.mark.asyncio
async def test_run_no_consumer_noop(settings: Any) -> None:
    consumer = KafkaConsumer(settings, topic="t")
    await consumer.run()  # returns early


@pytest.mark.asyncio
async def test_run_no_handler_noop(settings: Any) -> None:
    consumer = KafkaConsumer(settings, topic="t")
    consumer._consumer = MagicMock()  # type: ignore[assignment]
    await consumer.run()  # returns early (no handler)


# ============================================================
# RUN LOOP — with fake messages
# ============================================================
class _FakeMessage:
    """Minimal aiokafka message shape."""

    def __init__(self, value: Any, offset: int = 0) -> None:
        self.value = value
        self.offset = offset


class _FakeAsyncIterator:
    """Async iterator that yields messages, then flips owner._running=False.

    Semantics:
        `stop_after` = number of messages to yield BEFORE flipping the
        running flag. Default None → yield all messages without stopping.

    Why this design?
        Consumer's run() loop:
            async for msg in self._consumer:
                if not self._running: break   ← checked AFTER yield
                ...
        So to test "N messages processed", we must yield N+1 messages
        but flip _running=False at the last one. With stop_after=N,
        message N+1 will be yielded but loop breaks before handling it.
    """

    def __init__(self, messages: list[_FakeMessage], stop_after: int | None = None) -> None:
        self._messages = messages
        self._idx = 0
        self._stop_after = stop_after
        self._owner: KafkaConsumer | None = None

    def set_owner(self, owner: KafkaConsumer) -> None:
        self._owner = owner

    def __aiter__(self) -> _FakeAsyncIterator:
        return self

    async def __anext__(self) -> _FakeMessage:
        if self._idx >= len(self._messages):
            raise StopAsyncIteration

        # Flip running flag BEFORE yielding the NEXT message
        # so the consumer loop's `if not self._running: break` fires.
        if (
            self._stop_after is not None
            and self._idx >= self._stop_after
            and self._owner is not None
        ):
            self._owner._running = False

        msg = self._messages[self._idx]
        self._idx += 1
        return msg


@pytest.mark.asyncio
async def test_run_loop_calls_handler_for_dict_messages(
    enabled_settings: Any,
) -> None:
    """run() → handler called once per dict message; loop exits when stopped."""
    received: list[dict[str, Any]] = []

    async def handler(payload: dict[str, Any]) -> None:
        received.append(payload)

    # stop_after=2 → yield 2 messages; flip running flag on 3rd check
    # (there is no 3rd message, so StopAsyncIteration ends loop cleanly)
    fake_iter = _FakeAsyncIterator(
        messages=[
            _FakeMessage({"event_type": "a", "n": 1}, offset=0),
            _FakeMessage({"event_type": "b", "n": 2}, offset=1),
        ],
        stop_after=None,  # yield all
    )

    consumer = KafkaConsumer(enabled_settings, topic="t", handler=handler)
    consumer._consumer = fake_iter  # type: ignore[assignment]
    consumer._running = True
    fake_iter.set_owner(consumer)

    await consumer.run()

    assert received == [
        {"event_type": "a", "n": 1},
        {"event_type": "b", "n": 2},
    ]


@pytest.mark.asyncio
async def test_run_loop_skips_non_dict_values(
    enabled_settings: Any,
) -> None:
    """Non-dict message values are logged + skipped, loop continues."""
    received: list[dict[str, Any]] = []

    async def handler(payload: dict[str, Any]) -> None:
        received.append(payload)

    fake_iter = _FakeAsyncIterator(
        messages=[
            _FakeMessage("not-a-dict", offset=0),  # skipped
            _FakeMessage({"valid": True}, offset=1),
        ],
        stop_after=None,
    )

    consumer = KafkaConsumer(enabled_settings, topic="t", handler=handler)
    consumer._consumer = fake_iter  # type: ignore[assignment]
    consumer._running = True
    fake_iter.set_owner(consumer)

    await consumer.run()

    # Only the dict message reached handler
    assert received == [{"valid": True}]


@pytest.mark.asyncio
async def test_run_loop_handler_exception_isolated(
    enabled_settings: Any,
) -> None:
    """Handler raising → logged, loop continues to next message."""
    received: list[dict[str, Any]] = []

    async def handler(payload: dict[str, Any]) -> None:
        received.append(payload)
        if payload.get("fail"):
            raise RuntimeError("handler boom")

    fake_iter = _FakeAsyncIterator(
        messages=[
            _FakeMessage({"fail": True}, offset=0),
            _FakeMessage({"ok": True}, offset=1),
        ],
        stop_after=None,
    )

    consumer = KafkaConsumer(enabled_settings, topic="t", handler=handler)
    consumer._consumer = fake_iter  # type: ignore[assignment]
    consumer._running = True
    fake_iter.set_owner(consumer)

    await consumer.run()

    # Both messages delivered; handler exception did not stop loop
    assert received == [{"fail": True}, {"ok": True}]


@pytest.mark.asyncio
async def test_run_loop_breaks_when_running_flipped(
    enabled_settings: Any,
) -> None:
    """Explicit test: flip _running after 1st message; 2nd is not handled."""
    received: list[dict[str, Any]] = []

    async def handler(payload: dict[str, Any]) -> None:
        received.append(payload)

    # stop_after=1 → 1st yields, then _running=False.
    # 2nd yield attempt trips break before handler.
    fake_iter = _FakeAsyncIterator(
        messages=[
            _FakeMessage({"n": 1}, offset=0),
            _FakeMessage({"n": 2}, offset=1),  # will NOT reach handler
        ],
        stop_after=1,
    )

    consumer = KafkaConsumer(enabled_settings, topic="t", handler=handler)
    consumer._consumer = fake_iter  # type: ignore[assignment]
    consumer._running = True
    fake_iter.set_owner(consumer)

    await consumer.run()

    # Only 1st message handled; 2nd was yielded but loop broke
    assert received == [{"n": 1}]


@pytest.mark.asyncio
async def test_run_loop_outer_exception_logged(
    enabled_settings: Any,
) -> None:
    """If consumer iteration itself raises → logged, no re-raise."""

    class _ExplodingIterator:
        def __aiter__(self) -> _ExplodingIterator:
            return self

        async def __anext__(self) -> Any:
            raise RuntimeError("iteration boom")

    async def handler(_payload: dict[str, Any]) -> None:
        pass

    consumer = KafkaConsumer(enabled_settings, topic="t", handler=handler)
    consumer._consumer = _ExplodingIterator()  # type: ignore[assignment]
    consumer._running = True

    await consumer.run()  # should not raise


# ============================================================
# CONSTRUCTOR
# ============================================================
def test_consumer_custom_group_id(settings: Any) -> None:
    c = KafkaConsumer(settings, topic="t", group_id="custom-group")
    assert c._group_id == "custom-group"


def test_consumer_default_group_id(settings: Any) -> None:
    c = KafkaConsumer(settings, topic="t")
    assert c._group_id == settings.kafka_consumer_group


def test_consumer_topic_stored(settings: Any) -> None:
    c = KafkaConsumer(settings, topic="my-topic")
    assert c._topic == "my-topic"


def test_consumer_handler_optional(settings: Any) -> None:
    c = KafkaConsumer(settings, topic="t")
    assert c._handler is None
