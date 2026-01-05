"""Position indexing service for continuous tracking."""

import asyncio
import logging
import time
from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import Dict, List, Optional, Any

from bot.storage.redis_client import RedisClient
from bot.services.wallet_service import WalletService
from indexers.hyperliquid import HyperliquidClient, Position as HLPosition

logger = logging.getLogger(__name__)


@dataclass
class PositionSnapshot:
    """A snapshot of a wallet's positions at a point in time."""
    wallet: str
    exchange: str
    timestamp: float
    account_value: float
    total_pnl: float
    position_count: int
    positions: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PositionSnapshot":
        """Create from dictionary."""
        return cls(**data)


@dataclass
class TraderMetrics:
    """Computed metrics for a trader."""
    wallet: str
    exchange: str
    # Account info
    account_value: float
    # PnL metrics
    all_time_pnl: float
    day_7_roe: Optional[float]
    day_30_roe: Optional[float]
    day_90_roe: Optional[float]
    # Risk metrics
    max_drawdown: float
    profit_factor: float
    # Trade metrics
    total_trades: int
    win_rate: float
    winning_trades: int
    losing_trades: int
    trades_per_day: float
    direction_bias: str  # "Long Biased", "Short Biased", "Neutral"
    # Classification
    trader_type: str  # "Scalper", "Swing Trader", "Position Trader"
    account_age_days: int
    computed_at: float

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)


class PositionIndexer:
    """Indexes and tracks wallet positions over time."""

    POSITION_HISTORY_TTL = 90 * 24 * 3600  # 90 days
    SNAPSHOT_INTERVAL = 300  # 5 minutes between snapshots

    def __init__(
        self,
        redis_client: RedisClient,
        wallet_service: WalletService,
        hyperliquid_client: HyperliquidClient,
        poll_interval: float = 60.0,
    ):
        """Initialize position indexer.

        Args:
            redis_client: Redis client
            wallet_service: Wallet service for getting tracked wallets
            hyperliquid_client: Hyperliquid client
            poll_interval: Seconds between polling cycles
        """
        self.redis = redis_client
        self.wallet_service = wallet_service
        self.hl_client = hyperliquid_client
        self.poll_interval = poll_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        # Track all wallets being indexed
        self._indexed_wallets: Dict[str, float] = {}  # address -> last_indexed_at

    def _position_key(self, exchange: str, address: str) -> str:
        """Get Redis key for current positions."""
        return f"position:{exchange}:{address.lower()}:current"

    def _history_key(self, exchange: str, address: str) -> str:
        """Get Redis key for position history (sorted set)."""
        return f"position:{exchange}:{address.lower()}:history"

    def _metrics_key(self, exchange: str, address: str) -> str:
        """Get Redis key for computed metrics."""
        return f"metrics:{exchange}:{address.lower()}"

    async def start(self) -> None:
        """Start the position indexing loop."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Position indexer started")

    async def stop(self) -> None:
        """Stop the position indexing loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Position indexer stopped")

    async def _run_loop(self) -> None:
        """Main polling loop."""
        while self._running:
            try:
                await self._poll_all_wallets()
            except Exception as e:
                logger.error(f"Error in position indexer: {e}")

            await asyncio.sleep(self.poll_interval)

    async def _poll_all_wallets(self) -> None:
        """Poll positions for all tracked wallets."""
        # Get all unique wallet addresses that need indexing
        # For now, we'll track wallets that users have explicitly asked about
        # In the future, this could poll all tracked wallets

        wallets_to_poll = list(self._indexed_wallets.keys())

        if not wallets_to_poll:
            return

        logger.debug(f"Polling {len(wallets_to_poll)} wallets")

        now = time.time()
        for address in wallets_to_poll:
            last_indexed = self._indexed_wallets.get(address, 0)

            # Only snapshot at the configured interval
            if now - last_indexed < self.SNAPSHOT_INTERVAL:
                continue

            try:
                await self._index_hyperliquid_wallet(address)
                self._indexed_wallets[address] = now
            except Exception as e:
                logger.warning(f"Failed to index {address}: {e}")

    async def add_wallet_to_index(self, address: str) -> None:
        """Add a wallet to the indexing list.

        Args:
            address: Wallet address to start indexing
        """
        address_lower = address.lower()
        if address_lower not in self._indexed_wallets:
            self._indexed_wallets[address_lower] = 0
            logger.info(f"Added {address_lower} to position indexer")

    async def remove_wallet_from_index(self, address: str) -> None:
        """Remove a wallet from the indexing list.

        Args:
            address: Wallet address to stop indexing
        """
        address_lower = address.lower()
        self._indexed_wallets.pop(address_lower, None)

    async def _index_hyperliquid_wallet(self, address: str) -> None:
        """Index a Hyperliquid wallet's positions.

        Args:
            address: Wallet address
        """
        state = await self.hl_client.get_user_state(address)

        # Create snapshot
        snapshot = PositionSnapshot(
            wallet=address,
            exchange="hyperliquid",
            timestamp=time.time(),
            account_value=float(state.account_value),
            total_pnl=float(state.total_pnl),
            position_count=len(state.positions),
            positions=[
                {
                    "coin": p.coin,
                    "size": float(p.size),
                    "entry_px": float(p.entry_px),
                    "unrealized_pnl": float(p.unrealized_pnl),
                    "leverage": float(p.leverage) if p.leverage else None,
                    "direction": p.direction.value,
                }
                for p in state.positions
            ],
        )

        # Store current positions
        await self.redis.set(
            self._position_key("hyperliquid", address),
            snapshot.to_dict(),
            ex=3600,  # 1 hour TTL for current
        )

        # Add to history (sorted set with timestamp as score)
        history_key = self._history_key("hyperliquid", address)
        await self.redis.zadd(
            history_key,
            {str(snapshot.timestamp): snapshot.timestamp},
        )
        # Store the actual snapshot data
        await self.redis.set(
            f"{history_key}:{snapshot.timestamp}",
            snapshot.to_dict(),
            ex=self.POSITION_HISTORY_TTL,
        )

        # Prune old history entries
        cutoff = time.time() - self.POSITION_HISTORY_TTL
        await self.redis.redis.zremrangebyscore(history_key, 0, cutoff)

    async def get_current_snapshot(
        self,
        address: str,
        exchange: str = "hyperliquid",
    ) -> Optional[PositionSnapshot]:
        """Get the current position snapshot for a wallet.

        Args:
            address: Wallet address
            exchange: Exchange name

        Returns:
            PositionSnapshot or None
        """
        data = await self.redis.get(self._position_key(exchange, address))
        if data:
            return PositionSnapshot.from_dict(data)
        return None

    async def get_history_snapshots(
        self,
        address: str,
        exchange: str = "hyperliquid",
        since_hours: float = 24,
    ) -> List[PositionSnapshot]:
        """Get historical snapshots for a wallet.

        Args:
            address: Wallet address
            exchange: Exchange name
            since_hours: Hours of history to retrieve

        Returns:
            List of PositionSnapshot objects
        """
        history_key = self._history_key(exchange, address)
        cutoff = time.time() - (since_hours * 3600)

        # Get timestamps from sorted set
        timestamps = await self.redis.redis.zrangebyscore(
            history_key,
            cutoff,
            "+inf",
        )

        snapshots = []
        for ts in timestamps:
            data = await self.redis.get(f"{history_key}:{ts}")
            if data:
                snapshots.append(PositionSnapshot.from_dict(data))

        return sorted(snapshots, key=lambda s: s.timestamp)

    async def compute_trader_metrics(
        self,
        address: str,
        exchange: str = "hyperliquid",
    ) -> Optional[TraderMetrics]:
        """Compute comprehensive metrics for a trader.

        Args:
            address: Wallet address
            exchange: Exchange name

        Returns:
            TraderMetrics or None if insufficient data
        """
        # Get historical snapshots
        snapshots = await self.get_history_snapshots(address, exchange, since_hours=90*24)

        if len(snapshots) < 2:
            # Not enough history, fetch current and compute basic metrics
            if exchange == "hyperliquid":
                state = await self.hl_client.get_user_state(address)
                fills = await self.hl_client.get_user_fills(address, limit=200)

                # Compute basic metrics from fills
                winning = [f for f in fills if f.closed_pnl > 0]
                losing = [f for f in fills if f.closed_pnl < 0]
                win_rate = (len(winning) / len(fills) * 100) if fills else 0

                # Calculate profit factor
                total_profit = sum(float(f.closed_pnl) for f in winning)
                total_loss = abs(sum(float(f.closed_pnl) for f in losing))
                profit_factor = (total_profit / total_loss) if total_loss > 0 else float('inf')

                # Direction bias
                longs = [f for f in fills if "Long" in f.direction]
                shorts = [f for f in fills if "Short" in f.direction]
                if len(longs) > len(shorts) * 1.5:
                    direction_bias = "Long Biased"
                elif len(shorts) > len(longs) * 1.5:
                    direction_bias = "Short Biased"
                else:
                    direction_bias = "Neutral"

                # Trades per day (from recent fills)
                if fills:
                    oldest_fill = min(fills, key=lambda f: f.time_ms)
                    days_span = max(1, (time.time() * 1000 - oldest_fill.time_ms) / (1000 * 86400))
                    trades_per_day = len(fills) / days_span
                else:
                    trades_per_day = 0

                # Trader type classification
                if trades_per_day > 10:
                    trader_type = "Scalper"
                elif trades_per_day > 2:
                    trader_type = "Day Trader"
                elif trades_per_day > 0.5:
                    trader_type = "Swing Trader"
                else:
                    trader_type = "Position Trader"

                # Calculate realized PnL
                realized_pnl = sum(float(f.closed_pnl) for f in fills)

                return TraderMetrics(
                    wallet=address,
                    exchange=exchange,
                    account_value=float(state.account_value),
                    all_time_pnl=realized_pnl + float(state.total_pnl),
                    day_7_roe=None,  # Need history
                    day_30_roe=None,
                    day_90_roe=None,
                    max_drawdown=0.0,  # Need history
                    profit_factor=min(profit_factor, 99.99),  # Cap for display
                    total_trades=len(fills),
                    win_rate=win_rate,
                    winning_trades=len(winning),
                    losing_trades=len(losing),
                    trades_per_day=trades_per_day,
                    direction_bias=direction_bias,
                    trader_type=trader_type,
                    account_age_days=0,  # Would need first trade timestamp
                    computed_at=time.time(),
                )

            return None

        # With enough history, compute full metrics
        account_values = [s.account_value for s in snapshots]

        # Calculate ROE for different periods
        now = time.time()

        def calc_roe(hours: float) -> Optional[float]:
            cutoff = now - (hours * 3600)
            relevant = [s for s in snapshots if s.timestamp >= cutoff]
            if len(relevant) < 2:
                return None
            start_val = relevant[0].account_value
            end_val = relevant[-1].account_value
            if start_val <= 0:
                return None
            return ((end_val - start_val) / start_val) * 100

        day_7_roe = calc_roe(7 * 24)
        day_30_roe = calc_roe(30 * 24)
        day_90_roe = calc_roe(90 * 24)

        # Calculate max drawdown
        peak = account_values[0]
        max_dd = 0.0
        for val in account_values:
            if val > peak:
                peak = val
            drawdown = ((peak - val) / peak * 100) if peak > 0 else 0
            max_dd = max(max_dd, drawdown)

        # Get current state for other metrics
        current = snapshots[-1]

        return TraderMetrics(
            wallet=address,
            exchange=exchange,
            account_value=current.account_value,
            all_time_pnl=current.total_pnl,
            day_7_roe=day_7_roe,
            day_30_roe=day_30_roe,
            day_90_roe=day_90_roe,
            max_drawdown=max_dd,
            profit_factor=0.0,  # Would need trade-level data
            total_trades=0,
            win_rate=0.0,
            winning_trades=0,
            losing_trades=0,
            trades_per_day=0.0,
            direction_bias="Unknown",
            trader_type="Unknown",
            account_age_days=int((now - snapshots[0].timestamp) / 86400),
            computed_at=time.time(),
        )

    async def get_or_compute_metrics(
        self,
        address: str,
        exchange: str = "hyperliquid",
        cache_ttl: int = 300,
    ) -> Optional[TraderMetrics]:
        """Get cached metrics or compute fresh ones.

        Args:
            address: Wallet address
            exchange: Exchange name
            cache_ttl: Cache TTL in seconds

        Returns:
            TraderMetrics or None
        """
        # Check cache first
        cached = await self.redis.get(self._metrics_key(exchange, address))
        if cached:
            metrics = TraderMetrics(**cached)
            if time.time() - metrics.computed_at < cache_ttl:
                return metrics

        # Compute fresh metrics
        metrics = await self.compute_trader_metrics(address, exchange)

        if metrics:
            # Cache the results
            await self.redis.set(
                self._metrics_key(exchange, address),
                metrics.to_dict(),
                ex=cache_ttl,
            )
            # Also add to indexing for future tracking
            await self.add_wallet_to_index(address)

        return metrics
