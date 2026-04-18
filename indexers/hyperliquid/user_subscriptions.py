"""Keep HL user WS subscriptions in sync with watchlist ∪ top traders.

Runs alongside the HL WebSocket in the indexer process. Every
`refresh_interval` seconds, recomputes the desired subscription set from
Redis state and calls `ws.sync_user_subscriptions(desired)`.

Priority: user watchlist wallets are always subscribed. Top-trader slots
fill the rest up to `cap`. The cap exists to bound WS load — HL WS has no
documented per-connection cap, but at 2 subs per address (userFills +
userEvents) a 200-address cap = 400 WS subscriptions per connection, which
is well within observed limits.
"""

import asyncio
import logging
from typing import Protocol

logger = logging.getLogger(__name__)

DEFAULT_CAP = 200
DEFAULT_REFRESH_INTERVAL = 60.0
WATCHLIST_KEY_PATTERN = "wallet:*:subscribers"
TOP_TRADERS_KEY = "hl:top_traders"


class _RedisLike(Protocol):
    redis: object  # raw aioredis client for SCAN
    async def zrange(self, key: str, start: int, stop: int, *,
                     desc: bool = False) -> list[str]: ...


class _WSLike(Protocol):
    async def sync_user_subscriptions(self, addresses: set[str]) -> None: ...


class UserSubscriptionManager:
    """Periodic watchlist + leaderboard → WS subscription reconciler."""

    def __init__(
        self,
        redis_client: _RedisLike,
        ws: _WSLike,
        *,
        cap: int = DEFAULT_CAP,
        refresh_interval: float = DEFAULT_REFRESH_INTERVAL,
    ):
        self.redis = redis_client
        self.ws = ws
        self.cap = cap
        self.refresh_interval = refresh_interval
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_count = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "UserSubscriptionManager started — cap=%d, refresh=%.0fs",
            self.cap, self.refresh_interval,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def reconcile_once(self) -> set[str]:
        """Public entry for one-shot reconcile (used by tests + event triggers)."""
        desired = await self._compute_set()
        await self.ws.sync_user_subscriptions(desired)
        self._last_count = len(desired)
        return desired

    async def _run_loop(self) -> None:
        while self._running:
            try:
                addresses = await self.reconcile_once()
                logger.debug(
                    "UserSubscriptionManager: reconciled %d addresses",
                    len(addresses),
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("reconcile failed: %s", e)
            try:
                await asyncio.sleep(self.refresh_interval)
            except asyncio.CancelledError:
                break

    async def _compute_set(self) -> set[str]:
        watchlist = await self._scan_watchlist()
        remaining = max(0, self.cap - len(watchlist))
        if remaining == 0:
            return watchlist

        top_raw = await self.redis.zrange(
            TOP_TRADERS_KEY, 0, remaining - 1, desc=True,
        )
        top = {str(a).lower() for a in (top_raw or [])}
        return watchlist | top

    async def _scan_watchlist(self) -> set[str]:
        """Collect wallets that at least one user is tracking."""
        addresses: set[str] = set()
        async for key in self.redis.redis.scan_iter(match=WATCHLIST_KEY_PATTERN):
            # `wallet:{address}:subscribers` — address is the middle segment.
            if isinstance(key, bytes):
                key = key.decode("utf-8")
            parts = key.split(":")
            if len(parts) == 3 and parts[0] == "wallet":
                addresses.add(parts[1].lower())
        return addresses

    @property
    def last_count(self) -> int:
        return self._last_count
