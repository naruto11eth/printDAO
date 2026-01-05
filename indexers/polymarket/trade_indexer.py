"""Polymarket trade indexer with Redis persistence."""

import asyncio
import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional, Callable, Awaitable, List, Dict, Any

import redis.asyncio as aioredis

from .client import PolymarketClient
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
    unique_wallets: int = 0
    last_trade_time: Optional[str] = None
    uptime_seconds: float = 0.0
    errors: int = 0
    started_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class PolymarketTradeIndexer:
    """Perpetual trade indexer with Redis storage - indexes ALL trades."""

    TRADES_KEY = "pm:trades"
    TRADES_DATA_KEY = "pm:trade"
    WHALES_KEY = "pm:whales"
    WALLETS_KEY = "pm:wallets"
    STATS_KEY = "pm:indexer:stats"

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        whale_threshold: float = 10000.0,
        poll_interval: float = 2.0,
        trades_ttl: int = 86400 * 7,
        on_trade: Optional[Callable[[Trade], Awaitable[None]]] = None,
        on_whale: Optional[Callable[[Trade], Awaitable[None]]] = None,
    ):
        self.redis_url = redis_url
        self.whale_threshold = whale_threshold
        self.poll_interval = poll_interval
        self.trades_ttl = trades_ttl
        self.on_trade = on_trade
        self.on_whale = on_whale

        self.client = PolymarketClient()
        self._redis: Optional[aioredis.Redis] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._seen_tx: set = set()
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
        await self._load_seen_transactions()
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Polymarket trade indexer started - indexing ALL trades")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.client.close()
        if self._redis:
            await self._redis.close()

    async def _load_seen_transactions(self) -> None:
        try:
            r = await self._get_redis()
            recent = await r.zrevrange(self.TRADES_KEY, 0, 4999)
            self._seen_tx = set(recent)
            logger.info(f"Loaded {len(self._seen_tx)} existing PM transactions")
        except Exception as e:
            logger.warning(f"Could not load seen transactions: {e}")

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._poll_trades()
                await self._update_stats()
            except Exception as e:
                self._stats.errors += 1
                logger.error(f"PM indexer error: {e}")
            await asyncio.sleep(self.poll_interval)

    async def _poll_trades(self) -> None:
        try:
            trades = await self.client.get_trades(limit=500)
            now = time.time()
            new_count = 0

            for trade in trades:
                if trade.transaction_hash in self._seen_tx:
                    continue

                self._seen_tx.add(trade.transaction_hash)
                new_count += 1
                await self._store_trade(trade)
                self._minute_trades.append(now)
                self._hour_trades.append(now)
                self._stats.total_trades_indexed += 1
                self._stats.total_volume_usd += trade.usdc_size
                self._stats.last_trade_time = trade.timestamp_str

                if self.on_trade:
                    await self.on_trade(trade)

                if trade.usdc_size >= self.whale_threshold:
                    self._stats.whale_trades += 1
                    await self._store_whale(trade)
                    if self.on_whale:
                        await self.on_whale(trade)

            if new_count > 0:
                logger.debug(f"PM: Indexed {new_count} new trades")

        except Exception as e:
            self._stats.errors += 1
            logger.error(f"PM poll error: {e}")

    async def _store_trade(self, trade: Trade) -> None:
        r = await self._get_redis()
        trade_key = f"{self.TRADES_DATA_KEY}:{trade.transaction_hash}"
        trade_data = {
            "tx_hash": trade.transaction_hash,
            "timestamp": trade.timestamp,
            "timestamp_str": trade.timestamp_str,
            "market_slug": trade.market_slug or "",
            "outcome": trade.outcome or "",
            "side": trade.side.value if trade.side else "",
            "size": trade.size,
            "price": trade.price,
            "usdc_size": trade.usdc_size,
            "maker": trade.maker or "",
            "taker": trade.taker or "",
            "event_title": trade.event_title or "",
        }
        await r.hset(trade_key, mapping=trade_data)
        await r.expire(trade_key, self.trades_ttl)
        await r.zadd(self.TRADES_KEY, {trade.transaction_hash: trade.timestamp})
        if trade.taker:
            await r.sadd(self.WALLETS_KEY, trade.taker)

    async def _store_whale(self, trade: Trade) -> None:
        r = await self._get_redis()
        await r.zadd(self.WHALES_KEY, {trade.transaction_hash: trade.timestamp})

    async def _update_stats(self) -> None:
        now = time.time()
        self._minute_trades = [t for t in self._minute_trades if t > now - 60]
        self._hour_trades = [t for t in self._hour_trades if t > now - 3600]
        self._stats.trades_last_minute = len(self._minute_trades)
        self._stats.trades_last_hour = len(self._hour_trades)
        self._stats.uptime_seconds = now - self._start_time
        try:
            r = await self._get_redis()
            self._stats.unique_wallets = await r.scard(self.WALLETS_KEY)
            await r.hset(self.STATS_KEY, mapping={k: str(v) for k, v in self._stats.to_dict().items() if v is not None})
        except Exception:
            pass

    async def get_stats(self) -> IndexerStats:
        return self._stats

    async def get_recent_trades(self, limit: int = 100) -> List[Dict[str, Any]]:
        r = await self._get_redis()
        tx_hashes = await r.zrevrange(self.TRADES_KEY, 0, limit - 1)
        trades = []
        for tx_hash in tx_hashes:
            trade_data = await r.hgetall(f"{self.TRADES_DATA_KEY}:{tx_hash}")
            if trade_data:
                trades.append(trade_data)
        return trades

    async def get_trade_count(self) -> int:
        r = await self._get_redis()
        return await r.zcard(self.TRADES_KEY)
