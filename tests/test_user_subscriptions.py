"""Tests for HL WS dynamic user subscriptions, dispatcher, and manager."""

import json
from typing import Any

import pytest

from indexers.hyperliquid.user_events import (
    UserEventDispatcher,
    STREAM_NAME,
)
from indexers.hyperliquid.user_subscriptions import (
    UserSubscriptionManager,
    TOP_TRADERS_KEY,
    DEFAULT_CAP,
)
from indexers.hyperliquid.ws_client import HyperliquidWS


# ---------------- UserEventDispatcher ----------------

class _RecordingProducer:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def publish(self, stream_name: str, fields: dict[str, Any]) -> str:
        self.events.append((stream_name, dict(fields)))
        return "0-0"


class TestUserFills:
    async def test_publishes_on_valid_fills(self):
        p = _RecordingProducer()
        d = UserEventDispatcher(p)
        await d.handle_user_fills({
            "user": "0x" + "a" * 40,
            "isSnapshot": False,
            "fills": [{"coin": "BTC"}, {"coin": "ETH"}],
        })
        assert len(p.events) == 1
        stream, fields = p.events[0]
        assert stream == STREAM_NAME
        assert fields["address"] == "0x" + "a" * 40
        assert fields["event_type"] == "fills"
        assert fields["fills_count"] == 2
        assert fields["is_snapshot"] == "0"

    async def test_skips_empty_fills(self):
        p = _RecordingProducer()
        d = UserEventDispatcher(p)
        await d.handle_user_fills({
            "user": "0x" + "a" * 40,
            "fills": [],
        })
        assert p.events == []

    async def test_skips_missing_user(self):
        p = _RecordingProducer()
        d = UserEventDispatcher(p)
        await d.handle_user_fills({"fills": [{"coin": "BTC"}]})
        assert p.events == []

    async def test_rejects_non_address_string(self):
        p = _RecordingProducer()
        d = UserEventDispatcher(p)
        await d.handle_user_fills({"user": "not-hex", "fills": [{}]})
        assert p.events == []

    async def test_snapshot_flag_preserved(self):
        p = _RecordingProducer()
        d = UserEventDispatcher(p)
        await d.handle_user_fills({
            "user": "0x" + "b" * 40,
            "isSnapshot": True,
            "fills": [{}],
        })
        assert p.events[0][1]["is_snapshot"] == "1"


class TestLiquidationRouting:
    async def test_routes_liquidation_by_liquidated_user(self):
        p = _RecordingProducer()
        d = UserEventDispatcher(p)
        victim = "0x" + "c" * 40
        await d.handle_user_event({
            "liquidation": {
                "liquidator": "0x" + "d" * 40,
                "liquidated_user": victim,
                "liquidated_ntl_pos": "500000",
            },
        })
        stream, fields = p.events[0]
        assert stream == STREAM_NAME
        assert fields["event_type"] == "liquidation"
        assert fields["address"] == victim
        payload = json.loads(fields["payload"])
        assert payload["liquidation"]["liquidated_ntl_pos"] == "500000"

    async def test_accepts_camelcase_liquidated_user(self):
        """HL payloads have been observed with liquidatedUser too — accept both."""
        p = _RecordingProducer()
        d = UserEventDispatcher(p)
        await d.handle_user_event({
            "liquidation": {"liquidatedUser": "0x" + "e" * 40},
        })
        assert p.events[0][1]["address"] == "0x" + "e" * 40

    async def test_skips_liquidation_without_victim(self):
        p = _RecordingProducer()
        d = UserEventDispatcher(p)
        await d.handle_user_event({"liquidation": {"liquidator": "0xdead"}})
        assert p.events == []


class TestUserEventsFilteringFills:
    async def test_ignores_fills_from_user_events(self):
        """userEvents.fills has no user field — userFills is source of truth."""
        p = _RecordingProducer()
        d = UserEventDispatcher(p)
        await d.handle_user_event({"fills": [{"coin": "BTC"}]})
        assert p.events == []

    async def test_ignores_non_user_cancel(self):
        p = _RecordingProducer()
        d = UserEventDispatcher(p)
        await d.handle_user_event({"nonUserCancel": [{"oid": 1}]})
        assert p.events == []


class TestFundingRouting:
    async def test_publishes_funding_with_user(self):
        p = _RecordingProducer()
        d = UserEventDispatcher(p)
        await d.handle_user_event({
            "funding": {"user": "0x" + "f" * 40, "rate": "0.0001"},
        })
        assert p.events[0][1]["event_type"] == "funding"

    async def test_skips_funding_without_user(self):
        p = _RecordingProducer()
        d = UserEventDispatcher(p)
        await d.handle_user_event({"funding": {"rate": "0.0001"}})
        assert p.events == []


# ---------------- HyperliquidWS.sync_user_subscriptions ----------------

class _StubWS:
    """Stubs the websocket connection object."""
    def __init__(self):
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        self.closed = True


class TestSyncUserSubscriptions:
    async def test_adds_new_subs_on_live_connection(self):
        ws = HyperliquidWS()
        stub = _StubWS()
        ws._ws = stub
        ws._connected = True

        await ws.sync_user_subscriptions({"0xaaa", "0xbbb"})

        assert ws._user_subscriptions == {"0xaaa", "0xbbb"}
        # Each address → 2 subs (userFills + userEvents)
        assert len(stub.sent) == 4
        types = {m["subscription"]["type"] for m in stub.sent}
        assert types == {"userFills", "userEvents"}
        assert not stub.closed

    async def test_shrinking_set_forces_reconnect(self):
        ws = HyperliquidWS()
        stub = _StubWS()
        ws._ws = stub
        ws._connected = True
        ws._user_subscriptions = {"0xaaa", "0xbbb", "0xccc"}

        await ws.sync_user_subscriptions({"0xaaa"})

        assert ws._user_subscriptions == {"0xaaa"}
        assert stub.closed  # reconnect triggered

    async def test_unchanged_set_is_noop(self):
        ws = HyperliquidWS()
        stub = _StubWS()
        ws._ws = stub
        ws._connected = True
        ws._user_subscriptions = {"0xaaa"}

        await ws.sync_user_subscriptions({"0xaaa"})

        assert stub.sent == []
        assert not stub.closed

    async def test_queues_subs_when_not_connected(self):
        ws = HyperliquidWS()
        # No _ws, not connected — subs still tracked for the next _resubscribe
        await ws.sync_user_subscriptions({"0xaaa"})
        assert ws._user_subscriptions == {"0xaaa"}

    async def test_lowercases_addresses(self):
        ws = HyperliquidWS()
        stub = _StubWS()
        ws._ws = stub
        ws._connected = True

        await ws.sync_user_subscriptions({"0xABC"})
        assert ws._user_subscriptions == {"0xabc"}


# ---------------- UserSubscriptionManager ----------------

class _StubRedis:
    def __init__(self, watchlist_keys: list[str], top_traders: list[str]):
        self._watchlist_keys = watchlist_keys
        self._top = top_traders

        class _Raw:
            def __init__(self, keys):
                self._keys = keys

            async def scan_iter(self, match: str):
                for k in self._keys:
                    yield k

        self.redis = _Raw(watchlist_keys)

    async def zrange(self, key: str, start: int, stop: int, *, desc: bool = False):
        if key != TOP_TRADERS_KEY:
            return []
        # Simulate desc-order by reversing.
        ordered = list(reversed(self._top)) if desc else self._top
        if stop < 0:
            return ordered[start:]
        return ordered[start:stop + 1]


class _StubWSForMgr:
    def __init__(self):
        self.synced_to: set[str] | None = None

    async def sync_user_subscriptions(self, addresses: set[str]) -> None:
        self.synced_to = set(addresses)


class TestSubscriptionManager:
    async def test_watchlist_union_top(self):
        redis = _StubRedis(
            watchlist_keys=["wallet:0xwatch1:subscribers",
                            "wallet:0xwatch2:subscribers"],
            top_traders=["0xtop1", "0xtop2", "0xtop3"],
        )
        ws = _StubWSForMgr()
        mgr = UserSubscriptionManager(redis, ws, cap=10)  # type: ignore[arg-type]
        result = await mgr.reconcile_once()
        assert result == {"0xwatch1", "0xwatch2", "0xtop1", "0xtop2", "0xtop3"}
        assert ws.synced_to == result

    async def test_cap_trims_top_traders_keeps_watchlist(self):
        redis = _StubRedis(
            watchlist_keys=["wallet:0xwatch1:subscribers"],
            top_traders=["0xtop1", "0xtop2", "0xtop3", "0xtop4"],
        )
        ws = _StubWSForMgr()
        mgr = UserSubscriptionManager(redis, ws, cap=3)  # type: ignore[arg-type]
        result = await mgr.reconcile_once()
        assert "0xwatch1" in result
        assert len(result) == 3

    async def test_watchlist_exceeds_cap_drops_top(self):
        redis = _StubRedis(
            watchlist_keys=[f"wallet:0x{i:040x}:subscribers" for i in range(5)],
            top_traders=["0xtop1"],
        )
        ws = _StubWSForMgr()
        mgr = UserSubscriptionManager(redis, ws, cap=3)  # type: ignore[arg-type]
        result = await mgr.reconcile_once()
        # All 5 watchlist addresses still returned (we never drop user tracking),
        # no top-traders included.
        assert "0xtop1" not in result
        assert len(result) == 5

    async def test_ignores_malformed_keys(self):
        redis = _StubRedis(
            watchlist_keys=["wallet:bad", "other:0xaaa:subscribers",
                            "wallet:0xgood:subscribers"],
            top_traders=[],
        )
        ws = _StubWSForMgr()
        mgr = UserSubscriptionManager(redis, ws, cap=DEFAULT_CAP)  # type: ignore[arg-type]
        result = await mgr.reconcile_once()
        assert result == {"0xgood"}
