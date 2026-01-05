"""Hyperliquid trade indexer with Redis persistence."""

import asyncio
import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from decimal import Decimal
from typing import Optional, Callable, Awaitable, List, Dict, Any

import redis.asyncio as aioredis

from .client import HyperliquidClient
from .models import Trade

logger = logging.getLogger(__name__)


@dataclass
class IndexerStats:
    """Statistics for the trade indexer."""
    total_trades_indexed: int = 0
    trades_last_minute: int = 0
    trades_last_hour: int = 0
    whale_trades: int = 0
    total_volume_usd: float = 0.0
    coins_monitored: int = 0
    last_trade_time: Optional[str] = None
    uptime_seconds: float = 0.0
    errors: int = 0
    started_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class HyperliquidTradeIndexer:
    """Perpetual trade indexer with Redis storage - indexes ALL trades."""

    TRADES_KEY = "hl:trades"
    TRADES_DATA_KEY = "hl:trade"
    WHALES_KEY = "hl:whales"
    COINS_KEY = "hl:coins"
    STATS_KEY = "hl:indexer:stats"

    # Top coins to monitor (reduced to avoid rate limits)
    ALL_COINS = [
        "BTC", "ETH", "SOL", "DOGE", "WIF", "PEPE", "ARB", "SUI", "AVAX", "LINK",
        "XRP", "OP", "APT", "INJ", "TIA"
    ]

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        whale_threshold: float = 10_000_000.0,  # $10M for HL
        poll_interval: float = 5.0,  # Increased to avoid rate limits
        trades_ttl: int = 86400 * 7,
        coins: Optional[List[str]] = None,
        on_trade: Optional[Callable[[Trade], Awaitable[None]]] = None,
        on_whale: Optional[Callable[[Trade], Awaitable[None]]] = None,
    ):
        self.redis_url = redis_url
        self.whale_threshold = Decimal(str(whale_threshold))
        self.poll_interval = poll_interval
        self.trades_ttl = trades_ttl
        self.coins = coins or self.ALL_COINS
        self.on_trade = on_trade
        self.on_whale = on_whale

        self.client = HyperliquidClient()
        self._redis: Optional[aioredis.Redis] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._seen_tid: set = set()
        self._stats = IndexerStats()
        self._start_time = 0.0
        self._minute_trades: List[float] = []
        self._hour_trades: List[float] = []

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(self.redis_url, encoding="utf-8", decode_responses=True)
        return self._redis

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._start_time = time.time()
        self._stats.started_at = datetime.now().isoformat()
        self._stats.coins_monitored = len(self.coins)
        await self._load_seen_transactions()
        self._task = asyncio.create_task(self._run_loop())
        logger.info(f"Hyperliquid trade indexer started - monitoring {len(self.coins)} coins")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._redis:
            await self._redis.close()

    async def _load_seen_transactions(self) -> None:
        try:
            r = await self._get_redis()
            recent = await r.zrevrange(self.TRADES_KEY, 0, 9999)
            self._seen_tid = set(recent)
            logger.info(f"Loaded {len(self._seen_tid)} existing HL transactions")
        except Exception as e:
            logger.warning(f"Could not load seen transactions: {e}")

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._poll_all_coins()
                await self._update_stats()
            except Exception as e:
                self._stats.errors += 1
                logger.error(f"HL indexer error: {e}")
            await asyncio.sleep(self.poll_interval)

    async def _poll_all_coins(self) -> None:
        """Poll trades for all coins in batches to avoid rate limits."""
        batch_size = 5
        for i in range(0, len(self.coins), batch_size):
            batch = self.coins[i:i + batch_size]
            tasks = [self._poll_coin(coin) for coin in batch]
            await asyncio.gather(*tasks, return_exceptions=True)
            if i + batch_size < len(self.coins):
                await asyncio.sleep(1)  # Wait between batches

    async def _poll_coin(self, coin: str) -> None:
        try:
            trades = await self.client.get_recent_trades(coin, limit=100)
            now = time.time()

            for trade in trades:
                if trade.tid in self._seen_tid:
                    continue

                self._seen_tid.add(trade.tid)
                await self._store_trade(trade)
                self._minute_trades.append(now)
                self._hour_trades.append(now)
                self._stats.total_trades_indexed += 1
                self._stats.total_volume_usd += float(trade.notional_usd)
                self._stats.last_trade_time = trade.datetime.strftime("%H:%M:%S")

                if self.on_trade:
                    await self.on_trade(trade)

                if trade.notional_usd >= self.whale_threshold:
                    self._stats.whale_trades += 1
                    await self._store_whale(trade)
                    if self.on_whale:
                        await self.on_whale(trade)

        except Exception as e:
            logger.debug(f"Error polling {coin}: {e}")

    async def _store_trade(self, trade: Trade) -> None:
        r = await self._get_redis()
        trade_key = f"{self.TRADES_DATA_KEY}:{trade.tid}"
        trade_data = {
            "tid": trade.tid,
            "coin": trade.coin,
            "time_ms": trade.time_ms,
            "time_str": trade.datetime.strftime("%Y-%m-%d %H:%M:%S"),
            "price": str(trade.price),
            "size": str(trade.size),
            "side": trade.side.value,
            "notional_usd": str(trade.notional_usd),
            "direction": trade.direction.value,
            "hash": trade.hash,
            "taker": trade.users[0] if trade.users else "",
            "maker": trade.users[1] if len(trade.users) > 1 else "",
        }
        await r.hset(trade_key, mapping=trade_data)
        await r.expire(trade_key, self.trades_ttl)
        await r.zadd(self.TRADES_KEY, {trade.tid: trade.time_ms / 1000})
        await r.sadd(self.COINS_KEY, trade.coin)

    async def _store_whale(self, trade: Trade) -> None:
        r = await self._get_redis()
        await r.zadd(self.WHALES_KEY, {trade.tid: trade.time_ms / 1000})

    async def _update_stats(self) -> None:
        now = time.time()
        self._minute_trades = [t for t in self._minute_trades if t > now - 60]
        self._hour_trades = [t for t in self._hour_trades if t > now - 3600]
        self._stats.trades_last_minute = len(self._minute_trades)
        self._stats.trades_last_hour = len(self._hour_trades)
        self._stats.uptime_seconds = now - self._start_time

        # Limit seen set size
        if len(self._seen_tid) > 50000:
            self._seen_tid = set(list(self._seen_tid)[-25000:])

        try:
            r = await self._get_redis()
            await r.hset(self.STATS_KEY, mapping={k: str(v) for k, v in self._stats.to_dict().items() if v is not None})
        except Exception:
            pass

    async def get_stats(self) -> IndexerStats:
        return self._stats

    async def get_recent_trades(self, limit: int = 100, coin: Optional[str] = None) -> List[Dict[str, Any]]:
        r = await self._get_redis()
        tids = await r.zrevrange(self.TRADES_KEY, 0, limit * 2 - 1)
        trades = []
        for tid in tids:
            trade_data = await r.hgetall(f"{self.TRADES_DATA_KEY}:{tid}")
            if trade_data:
                if coin is None or trade_data.get("coin") == coin:
                    trades.append(trade_data)
                    if len(trades) >= limit:
                        break
        return trades

    async def get_trade_count(self) -> int:
        r = await self._get_redis()
        return await r.zcard(self.TRADES_KEY)
