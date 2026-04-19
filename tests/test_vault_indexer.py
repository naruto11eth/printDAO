"""Tests for the vault discovery + flow indexer."""

import httpx
import pytest

from indexers.hyperliquid.vault_indexer import (
    VaultIndexer,
    VAULTS_URL,
    HLP_ADDRESS,
    FLOW_ABS_THRESHOLD_USD,
    FLOW_REL_THRESHOLD,
    _extract_current_tvl,
    _parse_summaries,
)
from shared.constants import StreamName


def _summary(addr: str, tvl: float, name: str = "Test",
             leader: str = "0xleader", is_closed: bool = False) -> dict:
    return {
        "apr": 0.05,
        "summary": {
            "name": name,
            "vaultAddress": addr,
            "leader": leader,
            "tvl": str(tvl),
            "isClosed": is_closed,
            "relationship": {"type": "normal"},
            "createTimeMillis": 1_700_000_000_000,
        },
    }


def _details(tvl: float, follower_count: int = 10) -> dict:
    return {
        "name": "HLP",
        "vaultAddress": HLP_ADDRESS,
        "apr": 0.1,
        "portfolio": [
            ["day", {"accountValueHistory": [[100, "1000000"], [200, str(tvl)]]}],
        ],
        "followers": [{"user": f"0x{i:040x}"} for i in range(follower_count)],
    }


# ---------- _parse_summaries ----------

class TestParseSummaries:
    def test_parses_valid_entry(self):
        out = _parse_summaries([_summary("0x" + "a" * 40, 500_000)])
        assert len(out) == 1
        addr, tvl, summary, apr = out[0]
        assert addr == "0x" + "a" * 40
        assert tvl == 500_000.0
        assert apr == 0.05

    def test_rejects_non_hex_address(self):
        out = _parse_summaries([_summary("not-hex", 1)])
        assert out == []

    def test_lowercases_address(self):
        out = _parse_summaries([_summary("0x" + "A" * 40, 1)])
        assert out[0][0] == "0x" + "a" * 40

    def test_malformed_tvl_defaults_to_zero(self):
        entry = _summary("0x" + "a" * 40, 0)
        entry["summary"]["tvl"] = "nope"
        assert _parse_summaries([entry])[0][1] == 0.0

    def test_non_dict_entries_skipped(self):
        out = _parse_summaries([None, "string", _summary("0x" + "a" * 40, 1)])
        assert len(out) == 1


# ---------- _extract_current_tvl ----------

class TestExtractTvl:
    def test_picks_most_recent_from_day(self):
        assert _extract_current_tvl(
            {"portfolio": [["day", {"accountValueHistory": [[1, "100"], [2, "200"]]}]]}
        ) == 200.0

    def test_ignores_other_windows(self):
        assert _extract_current_tvl({
            "portfolio": [
                ["week", {"accountValueHistory": [[1, "999"]]}],
                ["day", {"accountValueHistory": [[1, "42"]]}],
            ],
        }) == 42.0

    def test_empty_portfolio_returns_zero(self):
        assert _extract_current_tvl({"portfolio": []}) == 0.0

    def test_missing_history_returns_zero(self):
        assert _extract_current_tvl(
            {"portfolio": [["day", {}]]}
        ) == 0.0


# ---------- stubs ----------

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
    def __init__(self, *, snapshots=None, top=None):
        self.cmds: list = []
        self._snapshots = snapshots or []
        self._top = top or []

    def pipeline(self, transaction=True):
        return _Pipe(self.cmds)

    async def zrevrange(self, key, start, stop):
        if "snapshots" in key:
            return list(self._snapshots[:stop - start + 1])
        if key == "hl:vaults":
            return list(self._top[:stop + 1])
        return []


class _FakeRedis:
    def __init__(self, *, snapshots=None, top=None):
        self.redis = _RawRedis(snapshots=snapshots, top=top)


class _FakeProducer:
    def __init__(self):
        self.events: list = []

    async def publish(self, stream: str, fields: dict) -> str:
        self.events.append((stream, dict(fields)))
        return "0-0"


class _FakeClient:
    def __init__(self, details_map: dict):
        self.details_map = details_map
        self.calls: list = []

    async def get_vault_details(self, vault_address: str) -> dict:
        self.calls.append(vault_address)
        return self.details_map.get(vault_address, {})


def _idx(redis, producer, client=None) -> VaultIndexer:
    return VaultIndexer(
        client=client or _FakeClient({}),
        redis_client=redis,
        producer=producer,
        top_n=3,
    )


# ---------- summaries persistence ----------

class TestSummariesPersistence:
    async def test_writes_scratch_rename_and_hashes(self):
        redis = _FakeRedis()
        producer = _FakeProducer()
        idx = _idx(redis, producer)
        entries = [
            ("0x" + "a" * 40, 1_000_000.0, {"name": "A", "leader": "0xL"}, 0.05),
            ("0x" + "b" * 40, 500_000.0, {"name": "B"}, 0.0),
        ]
        await idx._persist_summaries(entries)
        names = [c[0] for c in redis.redis.cmds]
        assert names[0] == "delete"       # clear scratch
        assert names[1] == "zadd"         # populate scratch
        assert names[2] == "rename"       # atomic swap
        assert names.count("hset") == 2   # summary hash per vault
        assert names.count("expire") == 2


# ---------- details persistence + flow ----------

class TestDetailsPersistence:
    async def test_first_snapshot_no_flow_event(self):
        redis = _FakeRedis(snapshots=[])
        producer = _FakeProducer()
        idx = _idx(redis, producer)
        await idx._store_details("0x" + "a" * 40, _details(500_000))
        assert producer.events == []

    async def test_large_absolute_delta_emits_flow(self):
        # Prior snapshot tvl = 10M; current tvl = 5M → delta 5M (≥$1M threshold),
        # rel = -50% (≥5% threshold)
        prev = f"1000:10000000:5"
        redis = _FakeRedis(snapshots=[prev])
        producer = _FakeProducer()
        idx = _idx(redis, producer)
        await idx._store_details("0x" + "a" * 40, _details(5_000_000))
        assert len(producer.events) == 1
        stream, fields = producer.events[0]
        assert stream == StreamName.VAULT_FLOW_HL
        assert fields["direction"] == "outflow"
        assert fields["delta_tvl"] == -5_000_000.0

    async def test_small_delta_skipped(self):
        prev = f"1000:1000000:5"
        redis = _FakeRedis(snapshots=[prev])
        producer = _FakeProducer()
        idx = _idx(redis, producer)
        # +$100k on a $1M vault = 10% rel but below $1M abs threshold
        await idx._store_details("0x" + "a" * 40, _details(1_100_000))
        assert producer.events == []

    async def test_hlp_uses_absolute_threshold_only(self):
        """HLP is huge — %-based filter would never trigger; use abs only."""
        prev = f"1000:400000000:100"  # $400M prior
        redis = _FakeRedis(snapshots=[prev])
        producer = _FakeProducer()
        idx = _idx(redis, producer)
        # -$2M on HLP = 0.5% rel, below FLOW_REL_THRESHOLD, but >$1M abs.
        await idx._store_details(HLP_ADDRESS, _details(398_000_000))
        assert len(producer.events) == 1
        assert producer.events[0][1]["vault_addr"] == HLP_ADDRESS

    async def test_non_hlp_requires_both_abs_and_rel(self):
        # $1M move on $1B vault = 0.1% rel — below FLOW_REL_THRESHOLD
        prev = f"1000:1000000000:5"
        redis = _FakeRedis(snapshots=[prev])
        producer = _FakeProducer()
        idx = _idx(redis, producer)
        await idx._store_details("0x" + "a" * 40, _details(999_000_000))
        assert producer.events == []


# ---------- poll_top_details_once ----------

class TestPollTopDetails:
    async def test_always_includes_hlp(self):
        # ZRANGE returns no vaults — HLP should still be queried.
        redis = _FakeRedis(top=[])
        producer = _FakeProducer()
        client = _FakeClient({HLP_ADDRESS: _details(100_000_000)})
        idx = _idx(redis, producer, client=client)
        count = await idx.poll_top_details_once()
        assert HLP_ADDRESS in client.calls
        assert count == 1

    async def test_no_duplicate_when_hlp_already_in_top(self):
        redis = _FakeRedis(top=[HLP_ADDRESS, "0xother"])
        producer = _FakeProducer()
        client = _FakeClient({
            HLP_ADDRESS: _details(100_000_000),
            "0xother": _details(1_000_000),
        })
        idx = _idx(redis, producer, client=client)
        await idx.poll_top_details_once()
        assert client.calls.count(HLP_ADDRESS) == 1


# ---------- poll_summaries_once end-to-end ----------

class TestPollSummaries:
    async def test_fetches_parses_persists(self):
        payload = [
            _summary("0x" + "a" * 40, 1_000_000),
            _summary("0x" + "b" * 40, 500_000),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url == httpx.URL(VAULTS_URL)
            return httpx.Response(200, json=payload)

        transport = httpx.MockTransport(handler)
        redis = _FakeRedis()
        producer = _FakeProducer()
        idx = _idx(redis, producer)

        # Patch AsyncClient to inject the mock transport
        import httpx as _httpx
        orig = _httpx.AsyncClient

        def mock_ac(*args, **kwargs):
            kwargs["transport"] = transport
            return orig(*args, **kwargs)

        _httpx.AsyncClient = mock_ac  # type: ignore[assignment]
        try:
            count = await idx.poll_summaries_once()
        finally:
            _httpx.AsyncClient = orig  # type: ignore[assignment]

        assert count == 2
        assert idx.last_summaries_count == 2
        # persistence calls happened
        cmd_names = [c[0] for c in redis.redis.cmds]
        assert "rename" in cmd_names
