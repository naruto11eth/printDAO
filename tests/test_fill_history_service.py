"""Tests for FillHistoryService — backfill, tail, reads."""

import json

import pytest

from bot.services.fill_history_service import (
    FillHistoryService,
    BACKFILL_PAGE_MAX,
    _extract_tid,
)


# ---------- shared stubs ----------

def _raw_fill(tid: int, coin: str = "BTC", px: str = "67000",
              sz: str = "0.1", time_ms: int = 1_700_000_000_000,
              closed_pnl: str = "0", side: str = "B") -> dict:
    return {
        "coin": coin,
        "side": side,
        "px": px,
        "sz": sz,
        "time": time_ms,
        "closedPnl": closed_pnl,
        "hash": f"0xhash{tid}",
        "fee": "1.5",
        "startPosition": "0",
        "dir": "Open Long",
        "oid": tid + 1000,
        "tid": tid,
        "crossed": True,
        "feeToken": "USDC",
    }


class _Pipe:
    def __init__(self, sink):
        self.sink = sink

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.sink.append((name, args, kwargs))
            return self
        return record

    async def execute(self):
        return []


class _RawRedis:
    def __init__(self, *, zrange_result=None, hgetall_map=None, scan_keys=None):
        self.cmds: list = []
        self._zrange_result = zrange_result or []
        self._hgetall_map = hgetall_map or {}
        self._scan_keys = scan_keys or []

    def pipeline(self, transaction=True):
        return _Pipe(self.cmds)

    async def zrevrange(self, key, start, stop):
        return list(self._zrange_result)

    async def zrevrangebyscore(self, key, max_, min_, *, start=0, num=-1):
        return list(self._zrange_result[:num])

    async def scan_iter(self, match):
        for k in self._scan_keys:
            yield k


class _FakeRedisClient:
    def __init__(self, *, zrange_result=None, hgetall_map=None, kv=None,
                 scan_keys=None):
        self.redis = _RawRedis(
            zrange_result=zrange_result, hgetall_map=hgetall_map,
            scan_keys=scan_keys,
        )
        self._kv: dict = kv or {}

    async def get(self, key):
        return self._kv.get(key)

    async def set(self, key, value, ex=None):
        self._kv[key] = value


class _FakeHLClient:
    """Stubs get_user_fills_by_time_raw for backfill tests."""
    def __init__(self, pages: list[list[dict]]):
        self._pages = pages  # Each call pops the next page
        self.calls: list = []

    async def get_user_fills_by_time_raw(self, address, start_time_ms, end_time_ms=None):
        self.calls.append((address, start_time_ms, end_time_ms))
        if not self._pages:
            return []
        return self._pages.pop(0)

    async def get_user_fills(self, address, limit=100):
        return []


# ---------- _extract_tid ----------

class TestExtractTid:
    def test_prefers_tid(self):
        assert _extract_tid({"tid": 123, "oid": 456, "hash": "0xabc"}) == "123"

    def test_falls_back_to_oid(self):
        assert _extract_tid({"oid": 456, "hash": "0xabc"}) == "456"

    def test_falls_back_to_hash(self):
        assert _extract_tid({"hash": "0xabc"}) == "0xabc"

    def test_empty_when_no_identifier(self):
        assert _extract_tid({}) == ""

    def test_treats_empty_string_as_missing(self):
        assert _extract_tid({"tid": "", "oid": 42}) == "42"


# ---------- _store_fills ----------

class TestStoreFills:
    async def test_writes_zadd_hset_expire_per_fill(self):
        redis = _FakeRedisClient()
        svc = FillHistoryService(None, redis)  # type: ignore[arg-type]

        await svc._store_fills("0xaaa", [_raw_fill(1), _raw_fill(2)])

        names = [c[0] for c in redis.redis.cmds]
        assert names.count("zadd") == 2
        assert names.count("hset") == 2
        assert names.count("expire") == 2
        assert svc.fills_persisted == 2

    async def test_skips_fills_without_id(self):
        redis = _FakeRedisClient()
        svc = FillHistoryService(None, redis)  # type: ignore[arg-type]
        # No tid, oid, or hash
        await svc._store_fills("0xaaa", [{"coin": "BTC", "sz": "0.1"}])
        assert redis.redis.cmds == []

    async def test_dict_fields_serialized_to_json(self):
        redis = _FakeRedisClient()
        svc = FillHistoryService(None, redis)  # type: ignore[arg-type]
        f = _raw_fill(1)
        f["liquidation"] = {"liquidatedUser": "0xdead"}
        await svc._store_fills("0xaaa", [f])
        # Find the hset call — mapping should have liquidation as JSON str
        hsets = [c for c in redis.redis.cmds if c[0] == "hset"]
        mapping = hsets[0][2]["mapping"] if "mapping" in hsets[0][2] else hsets[0][1][1]
        assert isinstance(mapping["liquidation"], str)
        assert "liquidatedUser" in mapping["liquidation"]


# ---------- handle_user_event ----------

class TestTail:
    async def test_persists_fills_event(self):
        redis = _FakeRedisClient()
        svc = FillHistoryService(None, redis)  # type: ignore[arg-type]

        fields = {
            "event_type": "fills",
            "address": "0x" + "a" * 40,
            "payload": json.dumps({"fills": [_raw_fill(1), _raw_fill(2)]}),
        }
        await svc.handle_user_event("stream:user_events:hl", fields)
        assert svc.fills_persisted == 2

    async def test_ignores_non_fills_event(self):
        redis = _FakeRedisClient()
        svc = FillHistoryService(None, redis)  # type: ignore[arg-type]
        await svc.handle_user_event("stream", {
            "event_type": "liquidation",
            "address": "0x" + "a" * 40,
            "payload": "{}",
        })
        assert redis.redis.cmds == []

    async def test_skips_invalid_address(self):
        redis = _FakeRedisClient()
        svc = FillHistoryService(None, redis)  # type: ignore[arg-type]
        await svc.handle_user_event("stream", {
            "event_type": "fills",
            "address": "nope",
            "payload": json.dumps({"fills": [_raw_fill(1)]}),
        })
        assert redis.redis.cmds == []

    async def test_skips_non_json_payload(self):
        redis = _FakeRedisClient()
        svc = FillHistoryService(None, redis)  # type: ignore[arg-type]
        await svc.handle_user_event("stream", {
            "event_type": "fills",
            "address": "0x" + "a" * 40,
            "payload": "not-json{",
        })
        assert redis.redis.cmds == []


# ---------- ensure_backfilled ----------

class TestEnsureBackfilled:
    async def test_skips_when_sentinel_exists(self):
        redis = _FakeRedisClient(kv={"hl:fills:backfill:0xaaa": "12345"})
        hl = _FakeHLClient(pages=[])
        svc = FillHistoryService(hl, redis)  # type: ignore[arg-type]
        ran = await svc.ensure_backfilled("0xaaa")
        assert ran is False
        assert hl.calls == []

    async def test_runs_when_no_sentinel(self):
        redis = _FakeRedisClient()
        hl = _FakeHLClient(pages=[[_raw_fill(1), _raw_fill(2)]])
        svc = FillHistoryService(hl, redis)  # type: ignore[arg-type]
        ran = await svc.ensure_backfilled("0xaaa")
        assert ran is True
        assert svc.backfills_ran == 1
        # Sentinel set
        assert await redis.get("hl:fills:backfill:0xaaa")

    async def test_force_bypasses_sentinel(self):
        redis = _FakeRedisClient(kv={"hl:fills:backfill:0xaaa": "old"})
        hl = _FakeHLClient(pages=[[_raw_fill(1)]])
        svc = FillHistoryService(hl, redis)  # type: ignore[arg-type]
        assert await svc.ensure_backfilled("0xaaa", force=True) is True

    async def test_raises_without_hl_client(self):
        redis = _FakeRedisClient()
        svc = FillHistoryService(None, redis)  # type: ignore[arg-type]
        with pytest.raises(RuntimeError):
            await svc.ensure_backfilled("0xaaa")


# ---------- _do_backfill pagination ----------

class TestBackfillPagination:
    async def test_stops_on_empty_page(self):
        hl = _FakeHLClient(pages=[[_raw_fill(1)]])
        redis = _FakeRedisClient()
        svc = FillHistoryService(hl, redis)  # type: ignore[arg-type]
        total = await svc._do_backfill("0xaaa")
        assert total == 1

    async def test_stops_on_short_page(self):
        # Two short pages (< BACKFILL_PAGE_MAX) — stop after first
        hl = _FakeHLClient(pages=[[_raw_fill(1), _raw_fill(2)]])
        redis = _FakeRedisClient()
        svc = FillHistoryService(hl, redis)  # type: ignore[arg-type]
        total = await svc._do_backfill("0xaaa")
        assert total == 2
        assert len(hl.calls) == 1  # no second page requested

    async def test_paginates_when_page_is_full(self):
        import time
        now_ms = int(time.time() * 1000)
        # All fills within the last ~30 days so the pagination loop doesn't
        # stop on the "oldest <= start_ms" guard.
        full_page = [_raw_fill(i, time_ms=now_ms - (i * 1000))
                     for i in range(BACKFILL_PAGE_MAX)]
        short_page = [_raw_fill(BACKFILL_PAGE_MAX,
                                time_ms=now_ms - (BACKFILL_PAGE_MAX * 1000) - 1000)]
        hl = _FakeHLClient(pages=[full_page, short_page])
        redis = _FakeRedisClient()
        svc = FillHistoryService(hl, redis, backfill_window_days=365)  # type: ignore[arg-type]
        total = await svc._do_backfill("0xaaa")
        assert total == BACKFILL_PAGE_MAX + 1
        assert len(hl.calls) == 2
        # Second call's end_time_ms is strictly older than first
        assert hl.calls[1][2] is not None and hl.calls[1][2] < hl.calls[0][2]


# ---------- get_fills ----------

class TestGetFills:
    async def test_reads_from_redis(self):
        hgetall_data = [
            {"coin": "BTC", "side": "B", "sz": "0.1", "px": "67000",
             "time": "1700", "closedPnl": "50", "hash": "0xabc",
             "fee": "1", "startPosition": "0", "dir": "Open Long"},
            {"coin": "ETH", "side": "A", "sz": "1", "px": "3500",
             "time": "1600", "closedPnl": "-20", "hash": "0xdef",
             "fee": "0.5", "startPosition": "0", "dir": "Close Long"},
        ]

        class _R(_RawRedis):
            def __init__(self):
                super().__init__(zrange_result=["1", "2"])
                self._idx = 0

            def pipeline(self, transaction=True):
                class _P:
                    def __init__(self_inner):
                        self_inner.calls = []

                    def __getattr__(self_inner, name):
                        def _(*a, **kw): return self_inner
                        return _

                    async def execute(self_inner):
                        return hgetall_data

                return _P()

        class _C:
            redis = _R()
            async def get(self, k): return None

        svc = FillHistoryService(None, _C())  # type: ignore[arg-type]
        fills = await svc.get_fills("0xaaa", limit=10)
        assert len(fills) == 2
        assert fills[0].coin == "BTC"

    async def test_empty_zrange_returns_empty_list(self):
        redis = _FakeRedisClient(zrange_result=[])
        svc = FillHistoryService(None, redis)  # type: ignore[arg-type]
        assert await svc.get_fills("0xaaa") == []


# ---------- _scan_once ----------

class TestScanLoop:
    async def test_triggers_backfill_for_wallets_without_sentinel(self):
        redis = _FakeRedisClient(
            scan_keys=[
                "wallet:0xaaa:subscribers",
                "wallet:0xbbb:subscribers",
            ],
            # 0xaaa already backfilled
            kv={"hl:fills:backfill:0xaaa": "1"},
        )
        hl = _FakeHLClient(pages=[[_raw_fill(1)]])
        svc = FillHistoryService(hl, redis)  # type: ignore[arg-type]
        triggered = await svc._scan_once()
        assert triggered == 1  # only 0xbbb

    async def test_ignores_malformed_keys(self):
        redis = _FakeRedisClient(
            scan_keys=["wallet:only2", "other:0xabc:subscribers"],
        )
        hl = _FakeHLClient(pages=[])
        svc = FillHistoryService(hl, redis)  # type: ignore[arg-type]
        triggered = await svc._scan_once()
        assert triggered == 0
