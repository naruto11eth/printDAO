"""Tests for the liquidation stream consumer."""

import json

import pytest

from bot.services.liquidation_indexer import (
    LiquidationIndexer,
    STREAM_IN,
    STREAM_OUT,
    ALERT_MIN_NTL,
)


class _FakePipe:
    def __init__(self, recorder: list):
        self.recorder = recorder

    def __getattr__(self, name):
        def _method(*args, **kwargs):
            self.recorder.append((name, args, kwargs))
            return self
        return _method

    async def execute(self):
        return []


class _FakeRawRedis:
    def __init__(self, recorder: list):
        self.recorder = recorder

    def pipeline(self, transaction: bool = True):
        return _FakePipe(self.recorder)


class _FakeRedisClient:
    def __init__(self):
        self.cmds: list = []
        self.redis = _FakeRawRedis(self.cmds)


class _FakeProducer:
    def __init__(self):
        self.events: list = []

    async def publish(self, stream_name, fields, **kw):
        self.events.append((stream_name, dict(fields)))
        return "0-0"


def _liq_fields(address: str, ntl_pos: float, *, ts: float = 1_700_000_000.0,
                account_value: float = 1_000_000.0,
                liquidator: str = "0xdead") -> dict[str, str]:
    return {
        "event_type": "liquidation",
        "address": address,
        "_published_at": str(ts),
        "payload": json.dumps({
            "liquidation": {
                "liquidator": liquidator,
                "liquidated_user": address,
                "liquidated_ntl_pos": str(ntl_pos),
                "liquidated_account_value": str(account_value),
            },
        }),
    }


class TestLiquidationIndexer:
    async def test_persists_and_reemits(self):
        redis = _FakeRedisClient()
        producer = _FakeProducer()
        idx = LiquidationIndexer(redis, producer)  # type: ignore[arg-type]

        await idx.handle(STREAM_IN, _liq_fields("0x" + "a" * 40, 500_000))

        cmd_names = [c[0] for c in redis.cmds]
        assert "hset" in cmd_names
        assert "zadd" in cmd_names
        assert "expire" in cmd_names
        assert "zremrangebyrank" in cmd_names

        assert len(producer.events) == 1
        stream, fields = producer.events[0]
        assert stream == STREAM_OUT
        assert fields["address"] == "0x" + "a" * 40
        assert fields["notable"] == "1"

    async def test_below_threshold_still_persists_but_not_notable(self):
        redis = _FakeRedisClient()
        producer = _FakeProducer()
        idx = LiquidationIndexer(redis, producer)  # type: ignore[arg-type]

        # $50k is below the $100k threshold
        await idx.handle(STREAM_IN, _liq_fields("0x" + "a" * 40, 50_000))
        # Still persisted
        assert any(c[0] == "hset" for c in redis.cmds)
        # Re-emitted but not notable
        assert producer.events[0][1]["notable"] == "0"

    async def test_skips_non_liquidation_event(self):
        redis = _FakeRedisClient()
        producer = _FakeProducer()
        idx = LiquidationIndexer(redis, producer)  # type: ignore[arg-type]

        await idx.handle(STREAM_IN, {
            "event_type": "fills",
            "address": "0xabc",
            "payload": "{}",
        })
        assert redis.cmds == []
        assert producer.events == []

    async def test_skips_invalid_address(self):
        redis = _FakeRedisClient()
        producer = _FakeProducer()
        idx = LiquidationIndexer(redis, producer)  # type: ignore[arg-type]

        await idx.handle(STREAM_IN, {
            "event_type": "liquidation",
            "address": "not-hex",
            "_published_at": "1",
            "payload": "{}",
        })
        assert redis.cmds == []

    async def test_skips_non_json_payload(self):
        redis = _FakeRedisClient()
        producer = _FakeProducer()
        idx = LiquidationIndexer(redis, producer)  # type: ignore[arg-type]

        await idx.handle(STREAM_IN, {
            "event_type": "liquidation",
            "address": "0x" + "a" * 40,
            "_published_at": "1",
            "payload": "not-json{",
        })
        assert redis.cmds == []

    async def test_accepts_camelcase_fields(self):
        """HL payloads observed with both snake_case and camelCase."""
        redis = _FakeRedisClient()
        producer = _FakeProducer()
        idx = LiquidationIndexer(redis, producer)  # type: ignore[arg-type]

        await idx.handle(STREAM_IN, {
            "event_type": "liquidation",
            "address": "0x" + "a" * 40,
            "_published_at": "1",
            "payload": json.dumps({
                "liquidation": {
                    "liquidator": "0xdead",
                    "liquidatedNtlPos": "200000",
                    "liquidatedAccountValue": "1500000",
                },
            }),
        })
        assert producer.events[0][1]["ntl_pos"] == 200000.0

    async def test_counts_and_last_ts(self):
        redis = _FakeRedisClient()
        producer = _FakeProducer()
        idx = LiquidationIndexer(redis, producer)  # type: ignore[arg-type]

        assert idx.indexed_count == 0
        await idx.handle(STREAM_IN, _liq_fields("0x" + "a" * 40, 500_000, ts=1001))
        await idx.handle(STREAM_IN, _liq_fields("0x" + "b" * 40, 200_000, ts=1002))
        assert idx.indexed_count == 2
        assert idx.last_event_ts == 1002.0

    async def test_threshold_exactly_at_boundary(self):
        redis = _FakeRedisClient()
        producer = _FakeProducer()
        idx = LiquidationIndexer(redis, producer)  # type: ignore[arg-type]

        await idx.handle(STREAM_IN, _liq_fields("0x" + "a" * 40, ALERT_MIN_NTL))
        # At the threshold counts as notable (>=, not >)
        assert producer.events[0][1]["notable"] == "1"
