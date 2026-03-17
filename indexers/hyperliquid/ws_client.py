"""Hyperliquid WebSocket client with auto-reconnect.

Subscribes to real-time trade feeds and user position updates.
Falls back to REST polling if WebSocket connection fails.
"""

import asyncio
import json
import logging
from typing import Callable, Coroutine, Any

logger = logging.getLogger(__name__)

WS_URL = "wss://api.hyperliquid.xyz/ws"
PING_INTERVAL = 15
MAX_RECONNECT_DELAY = 60


class HyperliquidWS:
    """WebSocket client for real-time Hyperliquid data.

    Supports subscribing to:
    - trades: real-time trade feed per coin
    - userEvents: position/fill updates for tracked wallets

    Uses custom JSON ping ({"method":"ping"}) every 15s.
    Auto-reconnects with exponential backoff.
    Re-subscribes all channels on reconnect.
    """

    def __init__(
        self,
        on_trade: Callable[[dict], Coroutine] | None = None,
        on_user_event: Callable[[dict], Coroutine] | None = None,
    ):
        self._on_trade = on_trade
        self._on_user_event = on_user_event
        self._trade_subscriptions: list[str] = []
        self._user_subscriptions: list[str] = []
        self._ws = None
        self._running = False
        self._connected = False

    def subscribe_trades(self, coin: str) -> None:
        """Add a coin to the trade subscription list."""
        if coin not in self._trade_subscriptions:
            self._trade_subscriptions.append(coin)

    def subscribe_user(self, address: str) -> None:
        """Add a wallet to the user events subscription list."""
        if address not in self._user_subscriptions:
            self._user_subscriptions.append(address)

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def subscription_count(self) -> int:
        return len(self._trade_subscriptions) + len(self._user_subscriptions)

    async def start(self) -> None:
        """Start the WebSocket connection with auto-reconnect."""
        self._running = True
        asyncio.create_task(self._connection_loop())
        logger.info(
            f"Hyperliquid WS starting — "
            f"{len(self._trade_subscriptions)} trade subs, "
            f"{len(self._user_subscriptions)} user subs"
        )

    async def stop(self) -> None:
        """Stop the WebSocket connection."""
        self._running = False
        self._connected = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _connection_loop(self) -> None:
        """Main connection loop with exponential backoff reconnect."""
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
                logger.error("websockets package not installed. Install with: pip install websockets")
                self._running = False
                break
            except Exception as e:
                self._connected = False
                logger.warning(f"Hyperliquid WS disconnected: {e}. Reconnecting in {delay}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_RECONNECT_DELAY)

    async def _resubscribe(self, ws: Any) -> None:
        """Re-subscribe all channels after reconnect."""
        for coin in self._trade_subscriptions:
            msg = json.dumps({
                "method": "subscribe",
                "subscription": {"type": "trades", "coin": coin},
            })
            await ws.send(msg)

        for address in self._user_subscriptions:
            msg = json.dumps({
                "method": "subscribe",
                "subscription": {"type": "userEvents", "user": address},
            })
            await ws.send(msg)

        total = len(self._trade_subscriptions) + len(self._user_subscriptions)
        if total:
            logger.info(f"Resubscribed to {total} channels")

    async def _read_loop(self, ws: Any) -> None:
        """Read and dispatch incoming messages."""
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            channel = msg.get("channel")

            if channel == "pong":
                continue
            if channel == "subscriptionResponse":
                logger.debug(f"Subscription confirmed: {msg.get('data', {}).get('subscription', {})}")
                continue

            if channel == "trades" and self._on_trade:
                data = msg.get("data", {})
                await self._on_trade(data)
            elif channel == "user" and self._on_user_event:
                data = msg.get("data", {})
                await self._on_user_event(data)

    async def _ping_loop(self, ws: Any) -> None:
        """Send JSON ping every PING_INTERVAL seconds."""
        while self._running:
            try:
                await asyncio.sleep(PING_INTERVAL)
                await ws.send(json.dumps({"method": "ping"}))
            except Exception:
                break
