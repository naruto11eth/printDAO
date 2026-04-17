"""Hyperliquid trade indexer — extends BaseTradeIndexer.

Ingest path is pluggable: REST polling (legacy) or WebSocket (Phase A).
Both paths funnel into `process_trade()` for dedup/persistence/callbacks.
"""

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
        rest_polling_enabled: bool = True,
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
        self.rest_polling_enabled = rest_polling_enabled

    async def start(self) -> None:
        await super().start()
        self._stats.extra["coins_monitored"] = len(self.coins)
        self._stats.extra["ingest_mode"] = "rest" if self.rest_polling_enabled else "ws"
        logger.info(
            "Hyperliquid trade indexer started — %d coins, ingest=%s",
            len(self.coins), self._stats.extra["ingest_mode"],
        )

    async def process_trade(self, trade: Trade) -> None:
        """Store + account for a trade. Called from REST and WS paths alike."""
        if not self._dedup.add(trade.tid):
            return

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

    async def ingest_ws_trades(self, payload: list[dict]) -> None:
        """Handler for HL WS `trades` channel. Expects list of raw trade dicts."""
        if not isinstance(payload, list):
            return
        for raw in payload:
            coin = raw.get("coin")
            if not coin:
                continue
            try:
                trade = Trade.from_raw(coin, raw)
            except (KeyError, ValueError, TypeError) as e:
                logger.debug("Skipping malformed WS trade: %s", e)
                continue
            await self.process_trade(trade)

    async def _poll(self) -> None:
        if not self.rest_polling_enabled:
            return
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
                await self.process_trade(trade)
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
        pipe = r.pipeline(transaction=True)
        pipe.hset(trade_key, mapping=trade_data)
        pipe.expire(trade_key, self.trades_ttl)
        pipe.zadd(self.trades_key, {trade.tid: trade.time_ms / 1000})
        pipe.sadd(self.COINS_KEY, trade.coin)
        await pipe.execute()

    async def get_recent_trades(
        self, limit: int = 100, coin: str | None = None,
    ) -> list[dict]:
        return await super().get_recent_trades(
            limit=limit,
            filter_field="coin" if coin else None,
            filter_value=coin,
        )
