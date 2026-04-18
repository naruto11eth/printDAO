"""Hyperliquid WebSocket client with auto-reconnect and dynamic user subs.

Subscribes to real-time trade feeds + user events for tracked wallets.
Custom JSON ping keeps the connection alive; exponential backoff reconnect
re-subscribes all channels on failure.

Dynamic user-subscription model (added Phase B2):
  - Every tracked address gets two subs: userFills and userEvents.
  - userFills response channel is `userFills`, includes `user` — reliable
    dispatch for fill events (Phase B5 fill history, C2 cluster detector).
  - userEvents response channel is `user`, lacks top-level user, but the
    `liquidation` variant contains `liquidated_user` — the only way to
    correlate liquidation events back to a wallet (Phase B3).
  - HL WS has no explicit unsubscribe: when the desired set shrinks, we
    drop the connection and let the reconnect loop rebuild it.
"""

import asyncio
import json
import logging
from typing import Callable, Coroutine

logger = logging.getLogger(__name__)

WS_URL = "wss://api.hyperliquid.xyz/ws"
PING_INTERVAL = 15
MAX_RECONNECT_DELAY = 60

Handler = Callable[[dict], Coroutine]


class HyperliquidWS:
    """WebSocket client for real-time Hyperliquid data.

    Channels:
      trades       — public market trades per coin
      user         — user events (liquidation/funding/fills/nonUserCancel)
      userFills    — user fills with `user` field (use for fill dispatch)
    """

    def __init__(
        self,
        on_trade: Handler | None = None,
        on_user_event: Handler | None = None,
        on_user_fills: Handler | None = None,
    ):
        self._on_trade = on_trade
        self._on_user_event = on_user_event
        self._on_user_fills = on_user_fills
        self._trade_subscriptions: set[str] = set()
        self._user_subscriptions: set[str] = set()
        self._ws = None
        self._running = False
        self._connected = False
        self._sub_lock = asyncio.Lock()

    def subscribe_trades(self, coin: str) -> None:
        self._trade_subscriptions.add(coin)

    def subscribe_user(self, address: str) -> None:
        """Back-compat single-add. Prefer sync_user_subscriptions()."""
        self._user_subscriptions.add(address.lower())

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def subscription_count(self) -> int:
        return len(self._trade_subscriptions) + len(self._user_subscriptions)

    @property
    def user_subscription_count(self) -> int:
        return len(self._user_subscriptions)

    async def sync_user_subscriptions(self, addresses: set[str]) -> None:
        """Reconcile live user subscriptions with the desired set.

        New addresses are subscribed on the open connection. When the set
        shrinks, closes the WS — the reconnect loop will subscribe only the
        new (smaller) set since HL has no per-sub unsubscribe.
        """
        desired = {a.lower() for a in addresses if a}
        async with self._sub_lock:
            current = set(self._user_subscriptions)
            added = desired - current
            removed = current - desired

            if not added and not removed:
                return

            if added and self._ws is not None and self._connected:
                for addr in added:
                    await self._send_user_subs(self._ws, addr)
                logger.info("HL WS: added %d user subscriptions", len(added))

            self._user_subscriptions = desired

            if removed and self._ws is not None:
                logger.info(
                    "HL WS: %d subs removed — dropping connection to rebuild",
                    len(removed),
                )
                try:
                    await self._ws.close()
                except Exception:
                    pass

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._connection_loop())
        logger.info(
            "Hyperliquid WS starting — %d trade subs, %d user subs",
            len(self._trade_subscriptions), len(self._user_subscriptions),
        )

    async def stop(self) -> None:
        self._running = False
        self._connected = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _connection_loop(self) -> None:
        delay = 1

        while self._running:
            try:
                import websockets
                async with websockets.connect(
                    WS_URL,
                    ping_interval=None,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    delay = 1
                    logger.info("Hyperliquid WS connected")

                    await self._resubscribe(ws)
                    await asyncio.gather(
                        self._read_loop(ws),
                        self._ping_loop(ws),
                    )
            except asyncio.CancelledError:
                break
            except ImportError:
                logger.error("websockets package not installed. pip install websockets")
                self._running = False
                break
            except Exception as e:
                self._connected = False
                logger.warning("Hyperliquid WS disconnected: %s. Reconnecting in %ds", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_RECONNECT_DELAY)

    async def _resubscribe(self, ws) -> None:
        async with self._sub_lock:
            for coin in self._trade_subscriptions:
                await ws.send(json.dumps({
                    "method": "subscribe",
                    "subscription": {"type": "trades", "coin": coin},
                }))
            for address in self._user_subscriptions:
                await self._send_user_subs(ws, address)

            total_trade = len(self._trade_subscriptions)
            total_user = len(self._user_subscriptions)
            if total_trade or total_user:
                logger.info(
                    "HL WS resubscribed: %d trades, %d users (2 subs each)",
                    total_trade, total_user,
                )

    @staticmethod
    async def _send_user_subs(ws, address: str) -> None:
        """Each tracked address gets two subs: userFills + userEvents."""
        for sub_type in ("userFills", "userEvents"):
            await ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": sub_type, "user": address},
            }))

    async def _read_loop(self, ws) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            channel = msg.get("channel")

            if channel == "pong":
                continue
            if channel == "subscriptionResponse":
                logger.debug("Subscription confirmed: %s",
                             msg.get("data", {}).get("subscription", {}))
                continue

            data = msg.get("data", {})

            if channel == "trades" and self._on_trade:
                await self._on_trade(data)
            elif channel == "user" and self._on_user_event:
                await self._on_user_event(data)
            elif channel == "userFills" and self._on_user_fills:
                await self._on_user_fills(data)

    async def _ping_loop(self, ws) -> None:
        while self._running:
            try:
                await asyncio.sleep(PING_INTERVAL)
                await ws.send(json.dumps({"method": "ping"}))
            except Exception:
                break
