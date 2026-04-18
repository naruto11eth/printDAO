"""Tests for the HL leaderboard crawler."""

import json

import httpx
import pytest

from indexers.hyperliquid.leaderboard_crawler import (
    LeaderboardCrawler,
    LEADERBOARD_URL,
    VALID_WINDOWS,
    _LeaderRow,
    _parse_rows,
)


def _raw_row(addr: str, all_time_pnl: float, display_name: str = "",
             account_value: float = 1_000_000.0) -> dict:
    return {
        "ethAddress": addr,
        "accountValue": str(account_value),
        "displayName": display_name,
        "prize": 0,
        "windowPerformances": [
            ["day", {"pnl": "1.0", "roi": "0.001", "vlm": "100"}],
            ["week", {"pnl": "10.0", "roi": "0.01", "vlm": "1000"}],
            ["month", {"pnl": "100.0", "roi": "0.1", "vlm": "10000"}],
            ["allTime", {"pnl": str(all_time_pnl), "roi": "0.5", "vlm": "1000000"}],
        ],
    }


class TestParseRows:
    def test_parses_well_formed_row(self):
        raw = [_raw_row("0xabc1", 500.0, display_name="Alice")]
        rows = _parse_rows(raw)
        assert len(rows) == 1
        r = rows[0]
        assert r.address == "0xabc1"
        assert r.display_name == "Alice"
        assert r.window_pnl("allTime") == 500.0
        assert r.window_pnl("day") == 1.0
        assert set(r.windows) == set(VALID_WINDOWS)

    def test_lowercases_address(self):
        raw = [_raw_row("0xABCDEF", 1.0)]
        assert _parse_rows(raw)[0].address == "0xabcdef"

    def test_skips_non_hex_addresses(self):
        raw = [{"ethAddress": "not-hex", "windowPerformances": []}]
        assert _parse_rows(raw) == []

    def test_handles_null_display_name(self):
        raw = [_raw_row("0xabc1", 1.0)]
        raw[0]["displayName"] = None
        assert _parse_rows(raw)[0].display_name == ""

    def test_skips_unknown_window(self):
        raw = [_raw_row("0xabc1", 1.0)]
        raw[0]["windowPerformances"].append(
            ["quarter", {"pnl": "999", "roi": "9", "vlm": "9"}],
        )
        rows = _parse_rows(raw)
        assert "quarter" not in rows[0].windows

    def test_continues_past_broken_row(self):
        raw = [
            _raw_row("0xabc1", 1.0),
            {"ethAddress": "0xbad", "windowPerformances": "nope"},
            _raw_row("0xabc2", 2.0),
        ]
        rows = _parse_rows(raw)
        assert len(rows) == 2
        assert {r.address for r in rows} == {"0xabc1", "0xabc2"}


class TestLeaderRowHash:
    def test_to_hash_has_all_window_fields(self):
        r = _parse_rows([_raw_row("0xabc1", 42.0)])[0]
        h = r.to_hash()
        for w in VALID_WINDOWS:
            assert f"{w}_pnl" in h
            assert f"{w}_roi" in h
            assert f"{w}_vlm" in h
        assert h["allTime_pnl"] == "42.00"
        assert h["address"] == "0xabc1"

    def test_hash_values_are_all_strings(self):
        r = _parse_rows([_raw_row("0xabc1", 42.0)])[0]
        h = r.to_hash()
        for v in h.values():
            assert isinstance(v, str)


class _Pipe:
    """In-memory stand-in for redis-py pipeline: records queued commands."""
    def __init__(self, calls: list):
        self.calls = calls

    def __getattr__(self, name):
        def _method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return _method


class _FakeRedisClient:
    """Minimal RedisClient stub used by the crawler's _persist."""
    def __init__(self):
        self.commands: list = []
        self.redis = None  # crawler doesn't use .redis directly in _persist

    def pipeline(self, *, transaction: bool = True):
        calls = self.commands

        class _Ctx:
            async def __aenter__(self_inner):
                self_inner._pipe = _Pipe(calls)
                return self_inner._pipe

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


class _FakeProducer:
    def __init__(self):
        self.events: list = []

    async def publish(self, stream_name, fields, *, maxlen=None):
        self.events.append((stream_name, dict(fields)))
        return "0-0"


class TestPersist:
    async def test_builds_zset_and_hashes(self):
        rows = _parse_rows([
            _raw_row("0xaaa", 10.0, display_name="A"),
            _raw_row("0xbbb", 5.0, display_name="B"),
        ])
        rows.sort(key=lambda r: r.window_pnl("allTime"), reverse=True)

        redis = _FakeRedisClient()
        crawler = LeaderboardCrawler(
            redis_client=redis,  # type: ignore[arg-type]
            producer=_FakeProducer(),  # type: ignore[arg-type]
            top_n=2,
            rank_by="allTime",
        )
        await crawler._persist(rows)

        names = [cmd[0] for cmd in redis.commands]
        assert "delete" in names
        assert "zadd" in names
        assert "rename" in names
        # Two hsets + two expires for two rows
        assert names.count("hset") == 2
        assert names.count("expire") == 2


class TestPollLoop:
    async def test_poll_once_end_to_end(self):
        payload = {
            "leaderboardRows": [
                _raw_row("0xaaa", 10.0, display_name="A"),
                _raw_row("0xbbb", 5.0, display_name="B"),
                _raw_row("0xccc", 1.0, display_name="C"),
            ],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url == httpx.URL(LEADERBOARD_URL)
            return httpx.Response(200, json=payload)

        transport = httpx.MockTransport(handler)
        redis = _FakeRedisClient()
        producer = _FakeProducer()
        crawler = LeaderboardCrawler(
            redis_client=redis,  # type: ignore[arg-type]
            producer=producer,  # type: ignore[arg-type]
            top_n=2,
            rank_by="allTime",
        )

        # Patch the fetch to use our mock transport.
        import httpx as _httpx
        orig = _httpx.AsyncClient

        def mock_ac(*args, **kwargs):
            kwargs["transport"] = transport
            return orig(*args, **kwargs)

        _httpx.AsyncClient = mock_ac  # type: ignore[assignment]
        try:
            await crawler._poll_once()
        finally:
            _httpx.AsyncClient = orig  # type: ignore[assignment]

        assert crawler.last_row_count == 2
        assert crawler.last_success_ts > 0

        # Stream event emitted with correct payload shape
        assert len(producer.events) == 1
        stream, fields = producer.events[0]
        assert stream == "stream:top_traders_updated:hl"
        assert fields["count"] == 2
        assert fields["rank_by"] == "allTime"


class TestConstruction:
    def test_rejects_unknown_rank_window(self):
        with pytest.raises(ValueError, match="rank_by must be"):
            LeaderboardCrawler(
                redis_client=_FakeRedisClient(),  # type: ignore[arg-type]
                producer=_FakeProducer(),  # type: ignore[arg-type]
                rank_by="forever",
            )
