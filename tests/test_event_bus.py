"""Tests for the generalized EventBusProducer in bot/services/stream_service.py.

Uses a minimal in-memory stub of RedisClient.xadd to avoid a real Redis.
"""

import pytest

from bot.services.stream_service import (
    EventBusProducer,
    TradeStreamProducer,
    TRADE_STREAM_KEY,
    DEFAULT_MAX_STREAM_LEN,
)


class _FakeRedisClient:
    def __init__(self):
        self.calls: list = []

    async def xadd(self, key, fields, maxlen=None):
        self.calls.append((key, dict(fields), maxlen))
        return f"{key}-{len(self.calls)}"


class TestEventBusProducer:
    async def test_publish_writes_to_arbitrary_stream(self):
        r = _FakeRedisClient()
        p = EventBusProducer(r)
        await p.publish("stream:test:foo", {"a": "1", "b": 2})
        assert len(r.calls) == 1
        key, fields, maxlen = r.calls[0]
        assert key == "stream:test:foo"
        assert fields["a"] == "1"
        assert fields["b"] == "2"  # coerced to str
        assert "_published_at" in fields
        assert maxlen == DEFAULT_MAX_STREAM_LEN

    async def test_publish_trade_uses_back_compat_key(self):
        r = _FakeRedisClient()
        p = EventBusProducer(r)
        await p.publish_trade("hl", {"tid": "1"})
        key, _, _ = r.calls[0]
        assert key == TRADE_STREAM_KEY.format(exchange="hl") == "stream:trades:hl"

    async def test_publish_whale_alert_tags_event_type(self):
        r = _FakeRedisClient()
        p = EventBusProducer(r)
        await p.publish_whale_alert("hl", {"tid": "1"})
        _, fields, _ = r.calls[0]
        assert fields["_event_type"] == "whale_alert"

    async def test_all_fields_coerced_to_str(self):
        r = _FakeRedisClient()
        p = EventBusProducer(r)
        await p.publish("s", {"i": 42, "f": 3.14, "b": True, "s": "x"})
        _, fields, _ = r.calls[0]
        for v in fields.values():
            assert isinstance(v, str)

    async def test_back_compat_alias(self):
        """TradeStreamProducer must remain a drop-in alias."""
        assert TradeStreamProducer is EventBusProducer
