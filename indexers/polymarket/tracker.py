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

    async def get_wallet_pnl(
        self,
        address: str,
        period: str = "all",
    ) -> PnLSummary:
        """Calculate PnL for a wallet over a time period.

        Args:
            address: Wallet address
            period: day, week, month, or all

        Returns:
            PnL summary
        """
        # Calculate time range
        now = datetime.now()
        if period == "day":
            start_time = now - timedelta(days=1)
        elif period == "week":
            start_time = now - timedelta(weeks=1)
        elif period == "month":
            start_time = now - timedelta(days=30)
        else:
            start_time = None

        # Get current positions
        positions = await self.client.get_all_positions(address)

        # Calculate totals from positions
        total_pnl = sum(p.cash_pnl for p in positions)
        realized_pnl = sum(p.realized_pnl for p in positions)
        unrealized_pnl = total_pnl - realized_pnl

        # Get trade activity for win/loss calculation
        start_ts = int(start_time.timestamp()) if start_time else None
        trades = await self.client.get_activity(
            address=address,
            activity_type="TRADE",
            start=start_ts,
            limit=500,
        )

        # Calculate win rate from resolved positions
        winning = sum(1 for p in positions if p.cash_pnl > 0)
        losing = sum(1 for p in positions if p.cash_pnl < 0)
        total = winning + losing
        win_rate = (winning / total * 100) if total > 0 else 0.0

        return PnLSummary(
            address=address,
            period=period,
            total_pnl=total_pnl,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            win_rate=win_rate,
            total_trades=len(trades),
            winning_trades=winning,
            losing_trades=losing,
            positions=positions,
        )

    async def detect_whale_trades(
        self,
        address: str,
        since_timestamp: Optional[int] = None,
    ) -> list[Activity]:
        """Detect whale trades for a wallet.

        Args:
            address: Wallet address
            since_timestamp: Only get trades after this timestamp

        Returns:
            List of whale trade activities
        """
        # Default to last 24 hours if no timestamp
        if since_timestamp is None:
            since_timestamp = int((datetime.now() - timedelta(hours=24)).timestamp())

        trades = await self.client.get_activity(
            address=address,
            activity_type="TRADE",
            start=since_timestamp,
            limit=500,
        )

        # Filter for whale trades
        whale_trades = [
            t for t in trades
            if t.usdc_size >= self.whale_threshold
        ]

        return whale_trades

    async def get_recent_whale_trades(
        self,
        min_size: Optional[float] = None,
        limit: int = 50,
    ) -> list[Trade]:
        """Get recent whale trades across all markets.

        Args:
            min_size: Override minimum trade size
            limit: Max trades to return

        Returns:
            List of whale trades
        """
        threshold = min_size if min_size else self.whale_threshold
        return await self.client.get_whale_trades(
            min_size=threshold,
            limit=limit,
        )

    async def watch_wallet(
        self,
        address: str,
        callback: Callable[[Activity], Awaitable[None]],
        poll_interval: float = 30.0,
    ) -> None:
        """Watch a wallet for new trades and call callback on whale trades.

        Args:
            address: Wallet address to watch
            callback: Async function to call on new whale trades
            poll_interval: Seconds between polls
        """
        # Initialize last seen timestamp
        if address not in self._last_seen_trades:
            self._last_seen_trades[address] = int(datetime.now().timestamp())

        self._running = True

        while self._running:
            try:
                # Get trades since last check
                trades = await self.client.get_activity(
                    address=address,
                    activity_type="TRADE",
                    start=self._last_seen_trades[address],
                    limit=100,
                )

                # Process new whale trades
                for trade in trades:
                    if trade.timestamp > self._last_seen_trades[address]:
                        if trade.usdc_size >= self.whale_threshold:
                            await callback(trade)
                        self._last_seen_trades[address] = trade.timestamp

                await asyncio.sleep(poll_interval)

            except Exception as e:
                logger.error(f"Error watching wallet {address}: {e}")
                await asyncio.sleep(poll_interval)

    def stop_watching(self) -> None:
        """Stop all wallet watching loops."""
        self._running = False

    async def get_top_positions(
        self,
        address: str,
        limit: int = 5,
    ) -> list[Position]:
        """Get top positions by value for a wallet.

        Args:
            address: Wallet address
            limit: Max positions to return

        Returns:
            Top positions by current value
        """
        positions = await self.get_wallet_positions(address)
        return positions[:limit]

    async def get_position_summary(self, address: str) -> dict:
        """Get a summary of wallet positions.

        Args:
            address: Wallet address

        Returns:
            Summary dict with key metrics
        """
        positions = await self.get_wallet_positions(address)
        portfolio_value = await self.client.get_portfolio_value(address)

        total_value = portfolio_value.total_value if portfolio_value else 0
        total_pnl = sum(p.cash_pnl for p in positions)
        total_invested = sum(p.initial_value for p in positions)

        winning = [p for p in positions if p.cash_pnl > 0]
        losing = [p for p in positions if p.cash_pnl < 0]

        return {
            "address": address,
            "total_value": total_value,
            "total_pnl": total_pnl,
            "total_invested": total_invested,
            "position_count": len(positions),
            "winning_positions": len(winning),
            "losing_positions": len(losing),
            "best_position": max(positions, key=lambda p: p.cash_pnl) if positions else None,
            "worst_position": min(positions, key=lambda p: p.cash_pnl) if positions else None,
            "positions": positions,
        }


class WhaleDetector:
    """Detects whale activity across Polymarket."""

    def __init__(
        self,
        client: PolymarketClient,
        whale_threshold: float = 10000.0,
    ):
        self.client = client
        self.whale_threshold = whale_threshold
        self._seen_tx_hashes: set[str] = set()
        self._running = False

    async def scan_for_whales(
        self,
        callback: Callable[[Trade], Awaitable[None]],
        poll_interval: float = 15.0,
    ) -> None:
        """Continuously scan for new whale trades.

        Args:
            callback: Async function to call on new whale trades
            poll_interval: Seconds between scans
        """
        self._running = True

        while self._running:
            try:
                trades = await self.client.get_whale_trades(
                    min_size=self.whale_threshold,
                    limit=50,
                )

                for trade in trades:
                    if trade.transaction_hash not in self._seen_tx_hashes:
                        self._seen_tx_hashes.add(trade.transaction_hash)
                        await callback(trade)

                # Limit memory usage - keep last 1000 tx hashes
                if len(self._seen_tx_hashes) > 1000:
                    # Remove oldest (arbitrary since set, but keeps bounded)
                    while len(self._seen_tx_hashes) > 500:
                        self._seen_tx_hashes.pop()

                await asyncio.sleep(poll_interval)

            except Exception as e:
                logger.error(f"Error scanning for whales: {e}")
                await asyncio.sleep(poll_interval)

    def stop(self) -> None:
        """Stop scanning."""
        self._running = False
