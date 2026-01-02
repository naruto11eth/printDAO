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
