"""Tests for the TWAP/cluster detector."""

import json

import pytest

from bot.services.cluster_detector import (
    ClusterDetector,
    STREAM_IN,
    STREAM_OUT,
    DEFAULT_MIN_FILLS,
)


def _fill(coin: str = "BTC", side: str = "B", sz: str = "0.1",
          px: str = "67000", time_ms: int = 1_700_000_000_000) -> dict:
    return {
        "coin": coin,
        "side": side,
        "sz": sz,
        "px": px,
        "time": time_ms,
        "tid": time_ms,  # unique enough for tests
    }


def _fields(address: str, fills: list[dict]) -> dict:
    return {
        "event_type": "fills",
        "address": address,
        "payload": json.dumps({"fills": fills}),
    }


class _Pipe:
    def __init__(self, sink):
        self.sink = sink

    def __getattr__(self, name):
        def _(*a, **kw):
            self.sink.append((name, a, kw))
            return self
        return _

    async def execute(self):
        return []


class _RawRedis:
    def __init__(self):
        self.cmds: list = []

    def pipeline(self, transaction=True):
        return _Pipe(self.cmds)


class _FakeRedis:
    def __init__(self):
        self.redis = _RawRedis()


class _FakeProducer:
    def __init__(self):
        self.events: list = []

    async def publish(self, stream: str, fields: dict) -> str:
        self.events.append((stream, dict(fields)))
        return "0-0"


def _det(**kw) -> ClusterDetector:
    params = {"window_seconds": 300, "min_fills": 5, "min_notional": 100_000}
    params.update(kw)
    return ClusterDetector(_FakeRedis(), _FakeProducer(), **params)


ADDR = "0x" + "a" * 40


class TestClusterDetection:
    async def test_below_min_fills_no_emit(self):
        d = _det()
        base_ts = 1_700_000_000_000
        # 4 fills, each $50k notional — under the 5-fill threshold
        fills = [_fill(sz="0.5", px="50000", time_ms=base_ts + i * 1000)
                 for i in range(4)]
        await d.handle(STREAM_IN, _fields(ADDR, fills))
        assert d.producer.events == []

    async def test_cluster_emitted_when_thresholds_met(self):
        d = _det()
        base_ts = 1_700_000_000_000
        # 5 fills × 0.5 BTC × $67k = $33,500 each → total $167,500
        fills = [_fill(sz="0.5", px="67000", time_ms=base_ts + i * 1000)
                 for i in range(5)]
        await d.handle(STREAM_IN, _fields(ADDR, fills))

        assert len(d.producer.events) == 1
        stream, fields = d.producer.events[0]
        assert stream == STREAM_OUT
        assert fields["address"] == ADDR
        assert fields["coin"] == "BTC"
        assert fields["side"] == "B"
        assert fields["direction"] == "accumulation"
        assert fields["fill_count"] == 5
        assert fields["total_notional"] == pytest.approx(33500.0 * 5)

    async def test_mixed_sides_block_emission(self):
        d = _det()
        base_ts = 1_700_000_000_000
        fills = [
            _fill(side="B", time_ms=base_ts + i * 1000)
            for i in range(4)
        ] + [_fill(side="A", time_ms=base_ts + 5000)]
        await d.handle(STREAM_IN, _fields(ADDR, fills))
        assert d.producer.events == []

    async def test_sell_side_cluster_marked_distribution(self):
        d = _det()
        base_ts = 1_700_000_000_000
        fills = [_fill(side="A", sz="0.5", px="67000",
                       time_ms=base_ts + i * 1000) for i in range(5)]
        await d.handle(STREAM_IN, _fields(ADDR, fills))
        assert d.producer.events[0][1]["direction"] == "distribution"

    async def test_window_prunes_old_fills(self):
        """Old fills outside the 5min window should not count."""
        d = _det()
        base_ts = 1_700_000_000_000
        # 4 old fills (30 min ago) + 4 new fills = 4 within window, under threshold
        old = [_fill(time_ms=base_ts - 30 * 60 * 1000 + i * 1000) for i in range(4)]
        new = [_fill(time_ms=base_ts + i * 1000) for i in range(4)]
        await d.handle(STREAM_IN, _fields(ADDR, old + new))
        assert d.producer.events == []

    async def test_resets_after_emission(self):
        """Emitting a cluster should clear state so the next fill doesn't re-fire."""
        d = _det()
        base_ts = 1_700_000_000_000
        fills = [_fill(sz="0.5", px="67000", time_ms=base_ts + i * 1000)
                 for i in range(5)]
        await d.handle(STREAM_IN, _fields(ADDR, fills))
        # Sixth fill just after — should NOT re-emit (state reset)
        await d.handle(STREAM_IN, _fields(ADDR, [
            _fill(sz="0.5", px="67000", time_ms=base_ts + 6000),
        ]))
        assert len(d.producer.events) == 1

    async def test_different_coins_tracked_independently(self):
        d = _det()
        base_ts = 1_700_000_000_000
        # 3 BTC + 3 ETH → neither hits 5-fill threshold
        fills = [_fill(coin="BTC", time_ms=base_ts + i * 1000) for i in range(3)]
        fills += [_fill(coin="ETH", time_ms=base_ts + i * 1000) for i in range(3)]
        await d.handle(STREAM_IN, _fields(ADDR, fills))
        assert d.producer.events == []
        assert d.tracked_keys == 2  # two (address, coin) buckets

    async def test_notional_floor_blocks_small_clusters(self):
        """5 tiny fills all same side — correct shape, too small in $ terms."""
        d = _det(min_notional=100_000)
        base_ts = 1_700_000_000_000
        # 5 × 0.001 BTC × $67k = $335 total — well under $100k
        fills = [_fill(sz="0.001", px="67000", time_ms=base_ts + i * 1000)
                 for i in range(5)]
        await d.handle(STREAM_IN, _fields(ADDR, fills))
        assert d.producer.events == []


class TestInputHardening:
    async def test_non_fills_event_ignored(self):
        d = _det()
        await d.handle(STREAM_IN, {
            "event_type": "liquidation",
            "address": ADDR,
            "payload": "{}",
        })
        assert d.producer.events == []

    async def test_non_hex_address_rejected(self):
        d = _det()
        await d.handle(STREAM_IN, {
            "event_type": "fills",
            "address": "not-hex",
            "payload": json.dumps({"fills": [_fill()]}),
        })
        assert d.producer.events == []

    async def test_malformed_json_skipped(self):
        d = _det()
        await d.handle(STREAM_IN, {
            "event_type": "fills",
            "address": ADDR,
            "payload": "not json{",
        })
        assert d.producer.events == []

    async def test_missing_coin_skipped(self):
        d = _det()
        bad = _fill()
        del bad["coin"]
        await d.handle(STREAM_IN, _fields(ADDR, [bad] * 5))
        assert d.producer.events == []

    async def test_invalid_side_skipped(self):
        d = _det()
        fills = [_fill(side="X", time_ms=1_700_000_000_000 + i * 1000)
                 for i in range(5)]
        await d.handle(STREAM_IN, _fields(ADDR, fills))
        assert d.producer.events == []

    async def test_non_list_fills_ignored(self):
        d = _det()
        await d.handle(STREAM_IN, {
            "event_type": "fills",
            "address": ADDR,
            "payload": json.dumps({"fills": "not-a-list"}),
        })
        assert d.producer.events == []


class TestPersistence:
    async def test_writes_hash_zsets_and_trims_global(self):
        d = _det()
        base_ts = 1_700_000_000_000
        fills = [_fill(sz="0.5", px="67000", time_ms=base_ts + i * 1000)
                 for i in range(5)]
        await d.handle(STREAM_IN, _fields(ADDR, fills))

        names = [c[0] for c in d.redis.redis.cmds]
        assert names.count("hset") == 1
        assert names.count("expire") == 1
        # two ZADDs — per-address and global — plus trim
        assert names.count("zadd") == 2
        assert names.count("zremrangebyrank") == 1
