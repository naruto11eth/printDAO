"""Hyperliquid trade indexer — extends BaseTradeIndexer."""

import asyncio
import logging
from decimal import Decimal
from typing import Callable, Awaitable

from indexers.base import BaseTradeIndexer
from .client import HyperliquidClient
from .models import Trade

logger = logging.getLogger(__name__)

ALL_COINS = [
    "BTC", "ETH", "SOL", "DOGE", "WIF", "PEPE", "ARB", "SUI", "AVAX", "LINK",
    "XRP", "OP", "APT", "INJ", "TIA",
]


class HyperliquidTradeIndexer(BaseTradeIndexer):
    """Indexes Hyperliquid perpetual trades to Redis."""

    COINS_KEY = "hl:coins"

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        whale_threshold: float = 10_000_000.0,
        poll_interval: float = 5.0,
        trades_ttl: int = 86400 * 7,
        coins: list[str] | None = None,
        on_trade: Callable[[Trade], Awaitable[None]] | None = None,
        on_whale: Callable[[Trade], Awaitable[None]] | None = None,
    ):
        super().__init__(
            redis_url=redis_url,
            trades_key="hl:trades",
            trades_data_key="hl:trade",
            whales_key="hl:whales",
            stats_key="hl:indexer:stats",
            whale_threshold=whale_threshold,
            poll_interval=poll_interval,
            trades_ttl=trades_ttl,
            on_trade=on_trade,
            on_whale=on_whale,
        )
        self.whale_threshold_decimal = Decimal(str(whale_threshold))
        self.coins = coins or ALL_COINS
        self.client = HyperliquidClient()

    async def start(self) -> None:
        await super().start()
        self._stats.extra["coins_monitored"] = len(self.coins)
        logger.info(f"Hyperliquid trade indexer started — monitoring {len(self.coins)} coins")

    async def _poll(self) -> None:
        batch_size = 5
        for i in range(0, len(self.coins), batch_size):
            batch = self.coins[i:i + batch_size]
            tasks = [self._poll_coin(coin) for coin in batch]
            await asyncio.gather(*tasks, return_exceptions=True)
            if i + batch_size < len(self.coins):
                await asyncio.sleep(1)

    async def _poll_coin(self, coin: str) -> None:
        try:
            trades = await self.client.get_recent_trades(coin, limit=100)

            for trade in trades:
                if not self._dedup.add(trade.tid):
                    continue

                await self._store_trade(trade)
                self._record_trade(
                    volume_usd=float(trade.notional_usd),
                    time_str=trade.datetime.strftime("%H:%M:%S"),
                )

                if self.on_trade:
                    await self.on_trade(trade)

                if trade.notional_usd >= self.whale_threshold_decimal:
                    self._record_whale()
                    await self.store_whale(trade.tid, trade.time_ms / 1000)
                    if self.on_whale:
                        await self.on_whale(trade)

        except Exception as e:
            logger.debug(f"Error polling {coin}: {e}")

    async def _store_trade(self, trade: Trade) -> None:
        r = await self._get_redis()
        trade_key = f"{self.trades_data_key}:{trade.tid}"
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
        await r.zadd(self.trades_key, {trade.tid: trade.time_ms / 1000})
        await r.sadd(self.COINS_KEY, trade.coin)

    async def get_recent_trades(
        self, limit: int = 100, coin: str | None = None,
    ) -> list[dict]:
        return await super().get_recent_trades(
            limit=limit,
            filter_field="coin" if coin else None,
            filter_value=coin,
        )
