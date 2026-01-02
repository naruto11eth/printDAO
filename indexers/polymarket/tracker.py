"""Polymarket position and whale tracker."""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Callable, Awaitable

from .client import PolymarketClient
from .models import Position, Trade, Activity, PnLSummary, ActivityType

logger = logging.getLogger(__name__)


class PolymarketTracker:
    """Tracks wallet positions and detects whale activity on Polymarket."""

    def __init__(
        self,
        client: PolymarketClient,
        whale_threshold: float = 10000.0,
    ):
        """Initialize tracker.

        Args:
            client: Polymarket API client
            whale_threshold: Minimum trade size for whale alerts (USD)
        """
        self.client = client
        self.whale_threshold = whale_threshold
        self._last_seen_trades: dict[str, int] = {}  # address -> last timestamp
        self._running = False

    async def get_wallet_positions(
        self,
        address: str,
        include_small: bool = False,
    ) -> list[Position]:
        """Get all positions for a wallet.

        Args:
            address: Wallet address
            include_small: Include positions < $1

        Returns:
            List of positions sorted by value
        """
        threshold = 0.0 if include_small else 1.0
        positions = await self.client.get_all_positions(
            address=address,
            size_threshold=threshold,
        )

        # Sort by current value descending
        return sorted(positions, key=lambda p: p.current_value, reverse=True)
