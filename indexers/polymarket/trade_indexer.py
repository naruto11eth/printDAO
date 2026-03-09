"""Polymarket trade indexer — extends BaseTradeIndexer."""

import logging
from typing import Callable, Awaitable

from indexers.base import BaseTradeIndexer
from .client import PolymarketClient
from .models import Trade

logger = logging.getLogger(__name__)


class PolymarketTradeIndexer(BaseTradeIndexer):
    """Indexes Polymarket trades to Redis."""

    WALLETS_KEY = "pm:wallets"

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        whale_threshold: float = 10_000.0,
        poll_interval: float = 2.0,
        trades_ttl: int = 86400 * 7,
        on_trade: Callable[[Trade], Awaitable[None]] | None = None,
        on_whale: Callable[[Trade], Awaitable[None]] | None = None,
    ):
        super().__init__(
            redis_url=redis_url,
            trades_key="pm:trades",
            trades_data_key="pm:trade",
            whales_key="pm:whales",
            stats_key="pm:indexer:stats",
            whale_threshold=whale_threshold,
            poll_interval=poll_interval,
            trades_ttl=trades_ttl,
            on_trade=on_trade,
            on_whale=on_whale,
        )
        self.client = PolymarketClient()

    async def start(self) -> None:
        await super().start()
        logger.info("Polymarket trade indexer started — indexing ALL trades")

    async def _on_stop(self) -> None:
        await self.client.close()

    async def _poll(self) -> None:
        try:
            trades = await self.client.get_trades(limit=500)
            new_count = 0

            for trade in trades:
                if not self._dedup.add(trade.transaction_hash):
                    continue

                new_count += 1
                await self._store_trade(trade)
                self._record_trade(
                    volume_usd=trade.usdc_size,
                    time_str=trade.datetime.strftime("%H:%M:%S"),
                )

                if self.on_trade:
                    await self.on_trade(trade)

                if trade.usdc_size >= self.whale_threshold:
                    self._record_whale()
                    await self.store_whale(trade.transaction_hash, trade.timestamp)
                    if self.on_whale:
                        await self.on_whale(trade)

            if new_count > 0:
                logger.debug(f"PM: Indexed {new_count} new trades")

        except Exception as e:
            self._stats.errors += 1
            logger.error(f"PM poll error: {e}")

    async def _store_trade(self, trade: Trade) -> None:
        r = await self._get_redis()
        trade_key = f"{self.trades_data_key}:{trade.transaction_hash}"
        trade_data = {
            "tx_hash": trade.transaction_hash,
            "timestamp": trade.timestamp,
            "timestamp_str": trade.datetime.strftime("%Y-%m-%d %H:%M:%S"),
            "market_slug": trade.slug or "",
            "outcome": trade.outcome or "",
            "side": trade.side.value if trade.side else "",
            "size": trade.size,
            "price": trade.price,
            "usdc_size": trade.usdc_size,
            "trader": trade.proxy_wallet or "",
            "event_title": trade.title or "",
        }
        await r.hset(trade_key, mapping=trade_data)
        await r.expire(trade_key, self.trades_ttl)
        await r.zadd(self.trades_key, {trade.transaction_hash: trade.timestamp})
        if trade.proxy_wallet:
            await r.sadd(self.WALLETS_KEY, trade.proxy_wallet)

    async def _update_extra_stats(self) -> None:
        try:
            r = await self._get_redis()
            self._stats.extra["unique_wallets"] = await r.scard(self.WALLETS_KEY)
        except Exception:
            pass
