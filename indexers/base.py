"""Base trade indexer with shared lifecycle, dedup, and stats."""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


class BoundedDedup:
    """Bounded deduplication set with FIFO eviction.

    Uses deque for insertion order + set for O(1) membership checks.
    Half the memory of OrderedDict for the same semantics.
    """

    __slots__ = ("_capacity", "_order", "_members")

    def __init__(self, capacity: int = 50_000) -> None:
        self._capacity = capacity
        self._order: deque[str] = deque(maxlen=capacity)
        self._members: set[str] = set()

    def __contains__(self, item: str) -> bool:
        return item in self._members

    def __len__(self) -> int:
        return len(self._members)

    def add(self, item: str) -> bool:
        """Add item. Returns True if new (not a duplicate)."""
        if item in self._members:
            return False
        if len(self._order) == self._capacity:
            evicted = self._order[0]
            self._members.discard(evicted)
        self._order.append(item)
        self._members.add(item)
        return True

    def load_existing(self, items: list[str]) -> None:
        """Bulk load from Redis on startup."""
        for item in items:
            self.add(item)


@dataclass
class IndexerStats:
    """Statistics for a trade indexer."""
    total_trades_indexed: int = 0
    trades_last_minute: int = 0
    trades_last_hour: int = 0
    whale_trades: int = 0
    total_volume_usd: float = 0.0
    last_trade_time: str | None = None
    uptime_seconds: float = 0.0
    errors: int = 0
    started_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.update(result.pop("extra", {}))
        return result


class BaseTradeIndexer:
    """Shared lifecycle, dedup, and stats for trade indexers.

    Subclasses must implement:
        _poll(): Fetch and process new trades
        _on_stop(): Exchange-specific cleanup (close clients, etc.)
    """

    def __init__(
        self,
        *,
        redis_url: str = "redis://localhost:6379/0",
        trades_key: str,
        trades_data_key: str,
        whales_key: str,
        stats_key: str,
        whale_threshold: float,
        poll_interval: float = 5.0,
        trades_ttl: int = 86400 * 7,
        dedup_capacity: int = 50_000,
        on_trade: Callable | None = None,
        on_whale: Callable | None = None,
    ):
        self.redis_url = redis_url
        self.trades_key = trades_key
        self.trades_data_key = trades_data_key
        self.whales_key = whales_key
        self.stats_key = stats_key
        self.whale_threshold = whale_threshold
        self.poll_interval = poll_interval
        self.trades_ttl = trades_ttl
        self.on_trade = on_trade
        self.on_whale = on_whale

        self._redis: aioredis.Redis | None = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._dedup = BoundedDedup(capacity=dedup_capacity)
        self._stats = IndexerStats()
        self._start_time = 0.0
        self._minute_trades: list[float] = []
        self._hour_trades: list[float] = []

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(
                self.redis_url, encoding="utf-8", decode_responses=True,
            )
        return self._redis

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._start_time = time.time()
        self._stats.started_at = datetime.now(tz=timezone.utc).isoformat()
        await self._load_seen_transactions()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._on_stop()
        if self._redis:
            await self._redis.close()

    async def _on_stop(self) -> None:
        """Override for exchange-specific cleanup."""

    async def _load_seen_transactions(self) -> None:
        try:
            r = await self._get_redis()
            recent = await r.zrevrange(self.trades_key, 0, 9999)
            self._dedup.load_existing(recent)
            logger.info(f"Loaded {len(self._dedup)} existing transactions for {self.trades_key}")
        except Exception as e:
            logger.warning(f"Could not load seen transactions: {e}")

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._poll()
                await self._update_stats()
            except Exception as e:
                self._stats.errors += 1
                logger.error(f"Indexer error ({self.trades_key}): {e}")
            await asyncio.sleep(self.poll_interval)

    async def _poll(self) -> None:
        """Fetch and process new trades. Must be implemented by subclasses."""
        raise NotImplementedError

    def _record_trade(self, volume_usd: float, time_str: str) -> None:
        """Record a trade in stats. Call from _poll() after storing."""
        now = time.time()
        self._minute_trades.append(now)
        self._hour_trades.append(now)
        self._stats.total_trades_indexed += 1
        self._stats.total_volume_usd += volume_usd
        self._stats.last_trade_time = time_str

    def _record_whale(self) -> None:
        """Record a whale trade in stats."""
        self._stats.whale_trades += 1

    async def store_whale(self, trade_id: str, score: float) -> None:
        """Store a whale trade ID in the whales sorted set."""
        r = await self._get_redis()
        await r.zadd(self.whales_key, {trade_id: score})

    async def _update_stats(self) -> None:
        now = time.time()
        self._minute_trades = [t for t in self._minute_trades if t > now - 60]
        self._hour_trades = [t for t in self._hour_trades if t > now - 3600]
        self._stats.trades_last_minute = len(self._minute_trades)
        self._stats.trades_last_hour = len(self._hour_trades)
        self._stats.uptime_seconds = now - self._start_time

        await self._update_extra_stats()
        await self._prune_sorted_sets()

        try:
            r = await self._get_redis()
            stats_dict = {
                k: str(v) for k, v in self._stats.to_dict().items() if v is not None
            }
            await r.hset(self.stats_key, mapping=stats_dict)
        except Exception as e:
            logger.debug(f"Failed to persist stats: {e}")

    async def _prune_sorted_sets(self) -> None:
        """Remove entries older than trades_ttl from trades and whales sorted sets.

        Prevents unbounded growth — trade data hashes have TTL but sorted set
        members remain as orphans without explicit pruning.
        """
        try:
            r = await self._get_redis()
            cutoff = time.time() - self.trades_ttl
            removed_trades = await r.zremrangebyscore(self.trades_key, 0, cutoff)
            removed_whales = await r.zremrangebyscore(self.whales_key, 0, cutoff)
            if removed_trades or removed_whales:
                logger.debug(
                    f"Pruned sorted sets: {removed_trades} trades, "
                    f"{removed_whales} whales older than {self.trades_ttl}s"
                )
        except Exception as e:
            logger.debug(f"Failed to prune sorted sets: {e}")

    async def _update_extra_stats(self) -> None:
        """Override to update exchange-specific stats before persistence."""

    async def get_stats(self) -> IndexerStats:
        return self._stats

    async def get_recent_trades(
        self, limit: int = 100, filter_field: str | None = None, filter_value: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get recent trades from Redis, optionally filtered.

        Uses pipeline for batch HGETALL (transaction=False for reads).
        """
        r = await self._get_redis()
        trade_ids = await r.zrevrange(self.trades_key, 0, limit * 2 - 1)

        if not trade_ids:
            return []

        pipe = r.pipeline(transaction=False)
        for tid in trade_ids:
            pipe.hgetall(f"{self.trades_data_key}:{tid}")
        results = await pipe.execute()

        trades = []
        for trade_data in results:
            if not trade_data:
                continue
            if filter_field and filter_value:
                if trade_data.get(filter_field) != filter_value:
                    continue
            trades.append(trade_data)
            if len(trades) >= limit:
                break
        return trades

    async def get_trade_count(self) -> int:
        r = await self._get_redis()
        return await r.zcard(self.trades_key)
