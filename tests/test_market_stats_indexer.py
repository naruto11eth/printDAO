"""Tests for the funding + OI time-series indexer."""

from dataclasses import dataclass
from decimal import Decimal

import pytest

from indexers.hyperliquid.market_stats_indexer import (
    MarketStatsIndexer,
    _RollingOI,
    OI_SPIKE_MIN_SAMPLES,
    OI_STATS_WINDOW_SECONDS,
)
from shared.constants import StreamName


@dataclass
class _Ctx:
    funding_rate: Decimal | None = None
    open_interest: Decimal | None = None


class _FakePipe:
    def __init__(self, sink):
        self.sink = sink

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.sink.append((name, args, kwargs))
            return self
        return record

    async def execute(self):
        return []


class _FakeRaw:
    def __init__(self, sink):
        self.sink = sink

    def pipeline(self, transaction=True):
        return _FakePipe(self.sink)


class _FakeRedis:
    def __init__(self):
        self.cmds: list = []
        self.redis = _FakeRaw(self.cmds)

    async def zadd(self, *a, **kw):
        return 0


class _FakeClient:
    def __init__(self, ctxs: dict):
        self._ctxs = ctxs

    async def get_asset_contexts(self) -> dict:
        return self._ctxs


class _FakeProducer:
    def __init__(self):
        self.events: list = []

    async def publish(self, stream: str, fields: dict) -> str:
        self.events.append((stream, dict(fields)))
        return "0-0"


def _idx(**kwargs) -> MarketStatsIndexer:
    ctxs = kwargs.pop("ctxs", {})
    return MarketStatsIndexer(
        _FakeClient(ctxs),
        _FakeRedis(),
        _FakeProducer(),
        poll_interval=0.01,
        **kwargs,
    )


class TestRollingOI:
    def test_push_and_prune(self):
        r = _RollingOI()
        r.push(0, 100)
        r.push(OI_STATS_WINDOW_SECONDS / 2, 200)
        r.push(OI_STATS_WINDOW_SECONDS + 1, 300)  # Pushes out t=0 entry
        assert len(r) == 2

    def test_stats_empty(self):
        r = _RollingOI()
        assert r.stats() == (0.0, 0.0)

    def test_stats_populated(self):
        r = _RollingOI()
        for v in (10, 20, 30):
            r.push(0, v)
        mean, std = r.stats()
        assert mean == 20.0
        assert std == pytest.approx(8.165, rel=1e-3)


class TestFundingEvents:
    async def test_rate_flip_positive_to_negative(self):
        idx = _idx()
        # Prime previous value
        idx._last_funding["BTC"] = 0.0001
        events = idx._detect_funding_events("BTC", -0.00005, now=1000.0)
        assert any(e["event_type"] == "rate_flip" for e in events)

    async def test_rate_flip_negative_to_positive(self):
        idx = _idx()
        idx._last_funding["ETH"] = -0.0001
        events = idx._detect_funding_events("ETH", 0.0001, now=1000.0)
        flip = [e for e in events if e["event_type"] == "rate_flip"][0]
        assert flip["prev_funding"] == -0.0001
        assert flip["funding"] == 0.0001

    async def test_no_flip_same_sign(self):
        idx = _idx()
        idx._last_funding["SOL"] = 0.0001
        events = idx._detect_funding_events("SOL", 0.00005, now=1000.0)
        assert not any(e["event_type"] == "rate_flip" for e in events)

    async def test_zero_funding_does_not_flag_flip(self):
        idx = _idx()
        idx._last_funding["DOGE"] = 0.0001
        events = idx._detect_funding_events("DOGE", 0.0, now=1000.0)
        assert not any(e["event_type"] == "rate_flip" for e in events)

    async def test_first_sample_does_not_flag_flip(self):
        idx = _idx()
        events = idx._detect_funding_events("BTC", -0.0001, now=1000.0)
        assert not any(e["event_type"] == "rate_flip" for e in events)

    async def test_extreme_funding_crossed(self):
        idx = _idx(extreme_funding_abs=0.001)
        events = idx._detect_funding_events("PEPE", 0.002, now=1000.0)
        extreme = [e for e in events if e["event_type"] == "extreme_funding"]
        assert extreme
        assert extreme[0]["funding"] == 0.002

    async def test_extreme_funding_below_threshold(self):
        idx = _idx(extreme_funding_abs=0.001)
        events = idx._detect_funding_events("ETH", 0.0005, now=1000.0)
        assert not any(e["event_type"] == "extreme_funding" for e in events)


class TestOIEvents:
    async def test_no_spike_without_enough_samples(self):
        idx = _idx()
        events = idx._detect_oi_events("BTC", 1_000_000.0, now=100.0)
        assert events == []

    async def test_spike_after_stable_history(self):
        idx = _idx(oi_spike_sigma=3.0)
        # Prime with OI_SPIKE_MIN_SAMPLES stable points
        for i in range(OI_SPIKE_MIN_SAMPLES):
            idx._detect_oi_events("BTC", 1_000_000.0 + i, now=float(i))
        # Now a 10x spike
        events = idx._detect_oi_events("BTC", 10_000_000.0, now=float(OI_SPIKE_MIN_SAMPLES))
        spike = [e for e in events if e["event_type"] == "oi_spike"]
        assert spike
        assert spike[0]["sigma"] > 3.0

    async def test_no_spike_within_normal_range(self):
        idx = _idx(oi_spike_sigma=3.0)
        for i in range(OI_SPIKE_MIN_SAMPLES):
            idx._detect_oi_events("ETH", 500_000.0 + i * 10, now=float(i))
        events = idx._detect_oi_events("ETH", 500_300.0, now=float(OI_SPIKE_MIN_SAMPLES))
        assert not any(e["event_type"] == "oi_spike" for e in events)

    async def test_flat_history_handles_zero_stddev(self):
        idx = _idx()
        # Identical values → stddev=0 → no divide-by-zero, no spike
        for i in range(OI_SPIKE_MIN_SAMPLES):
            idx._detect_oi_events("XRP", 100_000.0, now=float(i))
        events = idx._detect_oi_events("XRP", 100_000.0, now=float(OI_SPIKE_MIN_SAMPLES))
        assert events == []


class TestPollOnce:
    async def test_persists_and_emits_events(self):
        ctxs = {
            "BTC": _Ctx(funding_rate=Decimal("0.002"), open_interest=Decimal("1000")),
            "ETH": _Ctx(funding_rate=Decimal("0.0001"), open_interest=Decimal("500")),
        }
        idx = _idx(ctxs=ctxs, extreme_funding_abs=0.001)
        processed = await idx.poll_once()
        assert processed == 2

        # Pipeline writes for both coins
        names = [c[0] for c in idx.redis.cmds]
        assert names.count("zadd") == 4  # funding + oi per coin
        assert names.count("expire") == 4
        assert names.count("zremrangebyscore") == 4

        # BTC funding 0.002 exceeds threshold → event emitted
        events = idx.producer.events
        extreme = [e for e in events if e[1]["event_type"] == "extreme_funding"]
        assert extreme
        assert all(e[0] == StreamName.FUNDING_HL for e in events)

    async def test_skips_coins_with_missing_fields(self):
        ctxs = {"BTC": _Ctx(funding_rate=None, open_interest=None)}
        idx = _idx(ctxs=ctxs)
        processed = await idx.poll_once()
        assert processed == 0
        assert idx.redis.cmds == []

    async def test_counts_increment(self):
        ctxs = {"BTC": _Ctx(funding_rate=Decimal("0"), open_interest=Decimal("100"))}
        idx = _idx(ctxs=ctxs)
        await idx.poll_once()
        await idx.poll_once()
        assert idx.poll_count == 2
