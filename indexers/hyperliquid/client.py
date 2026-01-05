"""Hyperliquid API client."""

import asyncio
import logging
import time
from collections import defaultdict
from decimal import Decimal
from typing import Optional, Callable, List
from datetime import datetime, timedelta

from hyperliquid.info import Info
from hyperliquid.utils import constants

from .models import Trade, Position, Fill, AssetContext, UserState, SentimentSummary, TradeSide, Direction

logger = logging.getLogger(__name__)


class TradeCache:
    """Cache for trades with time-based pruning."""

    def __init__(self, lookback_seconds: int = 43200):  # 12 hours default
        self._store: dict[str, dict[str, Trade]] = defaultdict(dict)
        self._lookback_seconds = lookback_seconds

    def add(self, trade: Trade) -> bool:
        """Add trade to cache. Returns True if new."""
        coin_store = self._store[trade.coin]
        if trade.tid in coin_store:
            return False

        coin_store[trade.tid] = trade
        self._prune(trade.coin)
        return True

    def _prune(self, coin: str) -> None:
        """Remove old trades."""
        cutoff = int((datetime.now() - timedelta(seconds=self._lookback_seconds)).timestamp() * 1000)
        self._store[coin] = {
            tid: t for tid, t in self._store[coin].items()
            if t.time_ms >= cutoff
        }

    def recent(self, coin: str, limit: int = 100) -> List[Trade]:
        """Get recent trades for a coin."""
        trades = list(self._store.get(coin, {}).values())
        trades.sort(key=lambda t: t.time_ms, reverse=True)
        return trades[:limit]

    def all_recent(self, limit: int = 100) -> List[Trade]:
        """Get recent trades across all coins."""
        all_trades = []
        for coin_trades in self._store.values():
            all_trades.extend(coin_trades.values())
        all_trades.sort(key=lambda t: t.time_ms, reverse=True)
        return all_trades[:limit]

    def whales(self, threshold: Decimal = Decimal("10000000"), limit: int = 50) -> List[Trade]:
        """Get whale trades above threshold."""
        whale_trades = [
            t for coin_trades in self._store.values()
            for t in coin_trades.values()
            if t.notional_usd >= threshold
        ]
        whale_trades.sort(key=lambda t: t.time_ms, reverse=True)
        return whale_trades[:limit]


class HyperliquidClient:
    """Client for Hyperliquid API."""

    def __init__(
        self,
        testnet: bool = False,
        max_retries: int = 5,
        backoff_seconds: float = 0.5,
    ):
        """Initialize client.

        Args:
            testnet: Use testnet instead of mainnet
            max_retries: Max retry attempts for failed requests
            backoff_seconds: Initial backoff delay
        """
        self.base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self._info: Optional[Info] = None
        self._trade_cache = TradeCache()
        self._coins: List[str] = []

    def _get_info(self) -> Info:
        """Get or create Info instance."""
        if self._info is None:
            self._info = Info(base_url=self.base_url, skip_ws=True)
        return self._info

    def _retry_request(self, func: Callable, *args, **kwargs):
        """Execute request with retry logic."""
        delay = self.backoff_seconds

        for attempt in range(self.max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if "429" in str(e) and attempt < self.max_retries - 1:
                    logger.warning(f"Rate limited, waiting {delay}s...")
                    time.sleep(delay)
                    delay = min(delay * 2, 10)
                elif attempt < self.max_retries - 1:
                    logger.warning(f"Request failed: {e}, retrying...")
                    time.sleep(delay)
                else:
                    raise

    async def get_coins(self) -> List[str]:
        """Get list of available coins."""
        if self._coins:
            return self._coins

        def _fetch():
            info = self._get_info()
            meta = info.meta()
            return [asset["name"] for asset in meta.get("universe", [])]

        self._coins = await asyncio.to_thread(self._retry_request, _fetch)
        return self._coins

    async def get_recent_trades(self, coin: str, limit: int = 100) -> List[Trade]:
        """Get recent trades for a coin.

        Args:
            coin: Coin symbol (e.g., "BTC", "ETH")
            limit: Max trades to return

        Returns:
            List of Trade objects
        """
        def _fetch():
            info = self._get_info()
            raw = info.post("/info", {"type": "recentTrades", "coin": coin})
            return [Trade.from_raw(coin, item) for item in raw[:limit]]

        trades = await asyncio.to_thread(self._retry_request, _fetch)

        # Add to cache
        for t in trades:
            self._trade_cache.add(t)

        return trades

    async def get_user_state(self, address: str) -> UserState:
        """Get user state including positions.

        Args:
            address: Wallet address

        Returns:
            UserState object
        """
        def _fetch():
            info = self._get_info()
            raw = info.user_state(address)
            return UserState.from_raw(address, raw)

        return await asyncio.to_thread(self._retry_request, _fetch)

    async def get_user_positions(self, address: str) -> List[Position]:
        """Get user's open positions.

        Args:
            address: Wallet address

        Returns:
            List of Position objects
        """
        state = await self.get_user_state(address)
        return state.positions

    async def get_user_fills(self, address: str, limit: int = 50) -> List[Fill]:
        """Get user's recent trade fills.

        Args:
            address: Wallet address
            limit: Max fills to return

        Returns:
            List of Fill objects
        """
        def _fetch():
            info = self._get_info()
            raw = info.user_fills(address)
            fills = [Fill.from_raw(item) for item in raw]
            fills.sort(key=lambda f: f.time_ms, reverse=True)
            return fills[:limit]

        return await asyncio.to_thread(self._retry_request, _fetch)

    async def get_asset_contexts(self) -> dict[str, AssetContext]:
        """Get market context for all assets.

        Returns:
            Dict mapping coin to AssetContext
        """
        def _fetch():
            info = self._get_info()
            raw = info.meta_and_asset_ctxs()
            meta = raw[0]
            ctxs = raw[1]

            result = {}
            for i, asset in enumerate(meta.get("universe", [])):
                coin = asset["name"]
                if i < len(ctxs):
                    result[coin] = AssetContext.from_raw(coin, ctxs[i])

            return result

        return await asyncio.to_thread(self._retry_request, _fetch)

    async def get_whale_trades(
        self,
        coins: Optional[List[str]] = None,
        threshold: Decimal = Decimal("10000000"),
        limit: int = 50,
    ) -> List[Trade]:
        """Get recent whale trades.

        Args:
            coins: Coins to check (defaults to top coins)
            threshold: Min notional value for whale trades
            limit: Max trades to return

        Returns:
            List of whale Trade objects
        """
        if coins is None:
            # Default to most active coins
            coins = ["BTC", "ETH", "SOL", "DOGE", "WIF", "PEPE", "ARB", "SUI", "AVAX", "LINK"]

        all_trades = []

        for coin in coins:
            try:
                trades = await self.get_recent_trades(coin, limit=100)
                whale_trades = [t for t in trades if t.notional_usd >= threshold]
                all_trades.extend(whale_trades)
            except Exception as e:
                logger.warning(f"Failed to fetch trades for {coin}: {e}")

        # Sort by time and limit
        all_trades.sort(key=lambda t: t.time_ms, reverse=True)
        return all_trades[:limit]

    def get_cached_whales(self, threshold: Decimal = Decimal("10000000"), limit: int = 50) -> List[Trade]:
        """Get cached whale trades.

        Args:
            threshold: Min notional value
            limit: Max trades to return

        Returns:
            List of cached whale trades
        """
        return self._trade_cache.whales(threshold, limit)

    def get_cached_trades(self, coin: Optional[str] = None, limit: int = 100) -> List[Trade]:
        """Get cached trades.

        Args:
            coin: Optional coin filter
            limit: Max trades to return

        Returns:
            List of cached trades
        """
        if coin:
            return self._trade_cache.recent(coin, limit)
        return self._trade_cache.all_recent(limit)

    async def compute_sentiment(
        self,
        coin: str,
        lookback_hours: float = 12,
    ) -> SentimentSummary:
        """Compute market sentiment for a coin.

        Args:
            coin: Coin symbol
            lookback_hours: Hours to look back

        Returns:
            SentimentSummary object
        """
        trades = await self.get_recent_trades(coin, limit=500)

        # Filter by time
        cutoff = datetime.now() - timedelta(hours=lookback_hours)
        recent_trades = [t for t in trades if t.datetime >= cutoff]

        longs = [t for t in recent_trades if t.direction == Direction.LONG]
        shorts = [t for t in recent_trades if t.direction == Direction.SHORT]

        return SentimentSummary(
            coin=coin,
            period_hours=lookback_hours,
            total_trades=len(recent_trades),
            long_count=len(longs),
            short_count=len(shorts),
            long_volume=sum((t.notional_usd for t in longs), Decimal("0")),
            short_volume=sum((t.notional_usd for t in shorts), Decimal("0")),
        )
