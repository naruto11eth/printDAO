"""Tests for the WebSocket trade ingest path in HyperliquidTradeIndexer.

Verifies the new `process_trade` + `ingest_ws_trades` contract without touching
Redis — we stub `_store_trade` / `store_whale` to capture invocations.
"""

import pytest

from indexers.hyperliquid.trade_indexer import HyperliquidTradeIndexer


def _ws_trade(tid: int, coin: str = "BTC", px: str = "67000", sz: str = "0.1",
              side: str = "B", users=None) -> dict:
    return {
        "coin": coin,
        "side": side,
        "px": px,
        "sz": sz,
        "time": 1_700_000_000_000 + tid,
        "hash": f"0xabc{tid}",
        "tid": tid,
        "users": users or ["0xaaa", "0xbbb"],
    }


class _StubIndexer(HyperliquidTradeIndexer):
    """Bypass Redis — just record process_trade calls."""

    def __init__(self, **kwargs):
        super().__init__(rest_polling_enabled=False, **kwargs)
        self.stored: list = []
        self.whales: list = []
        self.stats_recorded = 0

    async def _store_trade(self, trade):
        self.stored.append(trade)

    async def store_whale(self, tid, score):
        self.whales.append((tid, score))

    def _record_trade(self, volume_usd, time_str):
        self.stats_recorded += 1

    def _record_whale(self):
        pass


class TestProcessTrade:
    async def test_stores_new_trade(self):
        idx = _StubIndexer()
        await idx.ingest_ws_trades([_ws_trade(tid=1)])
        assert len(idx.stored) == 1
        assert idx.stored[0].tid == "1"

    async def test_dedupes_repeated_tid(self):
        idx = _StubIndexer()
        await idx.ingest_ws_trades([_ws_trade(tid=1), _ws_trade(tid=1)])
        assert len(idx.stored) == 1

    async def test_skips_malformed_payload(self):
        idx = _StubIndexer()
        # Missing coin
        await idx.ingest_ws_trades([{"tid": 1}])
        assert idx.stored == []

    async def test_non_list_payload_is_ignored(self):
        idx = _StubIndexer()
        await idx.ingest_ws_trades({"not": "a list"})  # type: ignore[arg-type]
        assert idx.stored == []

    async def test_whale_threshold_triggers_store(self):
        # $67,000 × 1000 sz = $67M notional → above 10M default threshold
        idx = _StubIndexer(whale_threshold=10_000_000.0)
        await idx.ingest_ws_trades([_ws_trade(tid=7, sz="1000")])
        assert len(idx.whales) == 1

    async def test_whale_threshold_below_not_stored(self):
        # $67,000 × 0.01 sz = $670 notional
        idx = _StubIndexer(whale_threshold=10_000_000.0)
        await idx.ingest_ws_trades([_ws_trade(tid=8, sz="0.01")])
        assert idx.whales == []

    async def test_on_trade_callback_fired(self):
        seen = []

        async def on_trade(t):
            seen.append(t.tid)

        idx = _StubIndexer(on_trade=on_trade)
        await idx.ingest_ws_trades([_ws_trade(tid=42)])
        assert seen == ["42"]


class TestRestPollingGate:
    async def test_rest_disabled_is_noop(self):
        idx = _StubIndexer()  # rest_polling_enabled=False via subclass default
        assert idx.rest_polling_enabled is False
        await idx._poll()
        assert idx.stored == []
