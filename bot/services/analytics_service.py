"""Trader analytics service with TrySuper.co-style metrics."""

import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, Optional, Dict, Any

from indexers.hyperliquid import HyperliquidClient, Fill, Position
from bot.storage.redis_client import RedisClient

logger = logging.getLogger(__name__)


@dataclass
class TraderAnalytics:
    """TrySuper.co-style trader analytics."""

    address: str
    exchange: str

    # Classification
    trader_type: str  # "scalper", "day_trader", "swing_trader", "position_trader", "new_trader"
    account_age_days: int

    # Performance
    profit_factor: float
    all_time_pnl: float

    # ROE by timeframe
    roe_7d: float
    roe_30d: float
    roe_90d: float
    roe_all_time: float

    # Risk
    max_drawdown: float

    # Activity
    total_trades: int
    trades_per_day: float
    avg_trade_size: float
    direction_bias: float  # -1 (all short) to +1 (all long)
    win_rate: float
    avg_win: float
    avg_loss: float
    largest_win: float
    largest_loss: float
    last_trade_timestamp: float

    # Metadata
    calculated_at: float

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TraderAnalytics":
        """Create from dictionary."""
        return cls(**data)


class AnalyticsService:
    """Service for calculating trader analytics."""

    CACHE_KEY_PREFIX = "analytics"
    DEFAULT_CACHE_TTL = 300  # 5 minutes

    def __init__(
        self,
        hl_client: HyperliquidClient,
        redis: Optional[RedisClient] = None,
        cache_ttl: int = 300,
    ):
        """Initialize analytics service.

        Args:
            hl_client: Hyperliquid client
            redis: Redis client for caching
            cache_ttl: Cache TTL in seconds
        """
        self.hl_client = hl_client
        self.redis = redis
        self.cache_ttl = cache_ttl

    async def get_trader_analytics(
        self,
        address: str,
        exchange: str = "hyperliquid",
        force_refresh: bool = False,
    ) -> TraderAnalytics:
        """Get or calculate trader analytics.

        Args:
            address: Wallet address
            exchange: Exchange name
            force_refresh: Skip cache and recalculate

        Returns:
            TraderAnalytics object
        """
        cache_key = f"{self.CACHE_KEY_PREFIX}:{exchange}:{address.lower()}"

        # Check cache unless force refresh
        if not force_refresh and self.redis:
            cached = await self.redis.get(cache_key)
            if cached:
                try:
                    return TraderAnalytics.from_dict(cached)
                except Exception as e:
                    logger.warning(f"Failed to deserialize cached analytics: {e}")

        # Calculate fresh
        analytics = await self._calculate_analytics(address, exchange)

        # Cache result
        if self.redis:
            try:
                await self.redis.set(cache_key, analytics.to_dict(), ex=self.cache_ttl)
            except Exception as e:
                logger.warning(f"Failed to cache analytics: {e}")

        return analytics

    async def _calculate_analytics(
        self,
        address: str,
        exchange: str,
    ) -> TraderAnalytics:
        """Calculate all analytics metrics.

        Args:
            address: Wallet address
            exchange: Exchange name

        Returns:
            TraderAnalytics object
        """
        # Fetch data
        try:
            fills = await self.hl_client.get_user_fills(address, limit=2000)
            state = await self.hl_client.get_user_state(address)
        except Exception as e:
            logger.error(f"Failed to fetch data for {address}: {e}")
            return self._empty_analytics(address, exchange)

        if not fills:
            return self._empty_analytics(address, exchange)

        # Calculate all metrics
        account_age = self._calculate_account_age(fills)
        total_pnl = self._calculate_total_pnl(fills)
        profit_factor = self._calculate_profit_factor(fills)
        win_rate = self._calculate_win_rate(fills)
        max_dd = self._calculate_max_drawdown(fills)
        trades_per_day = self._calculate_trades_per_day(fills)
        direction_bias = self._calculate_direction_bias(fills)
        trader_type = self._classify_trader_type(fills, trades_per_day)
        avg_trade_size = self._calculate_avg_trade_size(fills)
        avg_win, avg_loss = self._calculate_avg_win_loss(fills)
        largest_win, largest_loss = self._calculate_largest_trades(fills)

        # ROE calculations
        roe_7d = self._calculate_roe(fills, state, days=7)
        roe_30d = self._calculate_roe(fills, state, days=30)
        roe_90d = self._calculate_roe(fills, state, days=90)
        roe_all = self._calculate_roe(fills, state, days=None)

        return TraderAnalytics(
            address=address,
            exchange=exchange,
            trader_type=trader_type,
            account_age_days=account_age,
            profit_factor=profit_factor,
            all_time_pnl=total_pnl,
            roe_7d=roe_7d,
            roe_30d=roe_30d,
            roe_90d=roe_90d,
            roe_all_time=roe_all,
            max_drawdown=max_dd,
            total_trades=len(fills),
            trades_per_day=trades_per_day,
            avg_trade_size=avg_trade_size,
            direction_bias=direction_bias,
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            largest_win=largest_win,
            largest_loss=largest_loss,
            last_trade_timestamp=fills[0].time_ms / 1000 if fills else 0,
            calculated_at=time.time(),
        )

    def _empty_analytics(self, address: str, exchange: str) -> TraderAnalytics:
        """Return empty analytics for address with no data."""
        return TraderAnalytics(
            address=address,
            exchange=exchange,
            trader_type="new_trader",
            account_age_days=0,
            profit_factor=0.0,
            all_time_pnl=0.0,
            roe_7d=0.0,
            roe_30d=0.0,
            roe_90d=0.0,
            roe_all_time=0.0,
            max_drawdown=0.0,
            total_trades=0,
            trades_per_day=0.0,
            avg_trade_size=0.0,
            direction_bias=0.0,
            win_rate=0.0,
            avg_win=0.0,
            avg_loss=0.0,
            largest_win=0.0,
            largest_loss=0.0,
            last_trade_timestamp=0,
            calculated_at=time.time(),
        )

    def _classify_trader_type(self, fills: List[Fill], trades_per_day: float) -> str:
        """Classify trader based on behavior patterns.

        Args:
            fills: List of fills
            trades_per_day: Average trades per day

        Returns:
            Trader type string
        """
        if len(fills) < 5:
            return "new_trader"

        # Also consider average hold time if we had position data
        # For now, use trade frequency as proxy
        if trades_per_day > 20:
            return "scalper"
        elif trades_per_day > 5:
            return "day_trader"
        elif trades_per_day > 1:
            return "swing_trader"
        else:
            return "position_trader"

    def _calculate_account_age(self, fills: List[Fill]) -> int:
        """Calculate account age in days from first trade.

        Args:
            fills: List of fills (sorted by time desc)

        Returns:
            Account age in days
        """
        if not fills:
            return 0

        # Find oldest fill
        oldest = min(fills, key=lambda f: f.time_ms)
        first_trade_time = datetime.fromtimestamp(oldest.time_ms / 1000)
        age = datetime.now() - first_trade_time
        return max(1, age.days)

    def _calculate_total_pnl(self, fills: List[Fill]) -> float:
        """Calculate total realized PnL.

        Args:
            fills: List of fills

        Returns:
            Total PnL in USD
        """
        return float(sum(f.closed_pnl for f in fills))

    def _calculate_profit_factor(self, fills: List[Fill]) -> float:
        """Calculate Profit Factor = Gross Profits / Gross Losses.

        Args:
            fills: List of fills

        Returns:
            Profit factor (>1 is profitable)
        """
        profits = sum(float(f.closed_pnl) for f in fills if float(f.closed_pnl) > 0)
        losses = abs(sum(float(f.closed_pnl) for f in fills if float(f.closed_pnl) < 0))

        if losses == 0:
            return float('inf') if profits > 0 else 0.0

        return round(profits / losses, 2)

    def _calculate_win_rate(self, fills: List[Fill]) -> float:
        """Calculate win rate percentage.

        Args:
            fills: List of fills

        Returns:
            Win rate as percentage (0-100)
        """
        if not fills:
            return 0.0

        # Only count fills that closed positions (have PnL)
        pnl_fills = [f for f in fills if float(f.closed_pnl) != 0]
        if not pnl_fills:
            return 0.0

        wins = sum(1 for f in pnl_fills if float(f.closed_pnl) > 0)
        return round((wins / len(pnl_fills)) * 100, 1)

    def _calculate_max_drawdown(self, fills: List[Fill]) -> float:
        """Calculate maximum drawdown from fill history.

        Args:
            fills: List of fills

        Returns:
            Max drawdown as percentage (0-100)
        """
        if not fills:
            return 0.0

        # Build equity curve from cumulative PnL
        sorted_fills = sorted(fills, key=lambda f: f.time_ms)
        equity = [0.0]
        for f in sorted_fills:
            equity.append(equity[-1] + float(f.closed_pnl))

        # Calculate drawdown
        peak = equity[0]
        max_dd = 0.0

        for value in equity:
            if value > peak:
                peak = value
            if peak > 0:
                dd = (peak - value) / peak
                max_dd = max(max_dd, dd)

        return round(max_dd * 100, 1)

    def _calculate_trades_per_day(self, fills: List[Fill]) -> float:
        """Calculate average trades per day.

        Args:
            fills: List of fills

        Returns:
            Average trades per day
        """
        if not fills:
            return 0.0

        days_active = self._calculate_account_age(fills)
        return round(len(fills) / max(days_active, 1), 1)

    def _calculate_direction_bias(self, fills: List[Fill]) -> float:
        """Calculate direction bias: -1 (all short) to +1 (all long).

        Args:
            fills: List of fills

        Returns:
            Direction bias
        """
        if not fills:
            return 0.0

        longs = sum(1 for f in fills if f.side.value == "B")
        shorts = len(fills) - longs

        return round((longs - shorts) / len(fills), 2)

    def _calculate_avg_trade_size(self, fills: List[Fill]) -> float:
        """Calculate average trade size in USD.

        Args:
            fills: List of fills

        Returns:
            Average trade size
        """
        if not fills:
            return 0.0

        total = sum(float(f.notional_usd) for f in fills)
        return round(total / len(fills), 2)

    def _calculate_avg_win_loss(self, fills: List[Fill]) -> tuple[float, float]:
        """Calculate average win and average loss.

        Args:
            fills: List of fills

        Returns:
            Tuple of (avg_win, avg_loss)
        """
        wins = [float(f.closed_pnl) for f in fills if float(f.closed_pnl) > 0]
        losses = [float(f.closed_pnl) for f in fills if float(f.closed_pnl) < 0]

        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0

        return round(avg_win, 2), round(avg_loss, 2)

    def _calculate_largest_trades(self, fills: List[Fill]) -> tuple[float, float]:
        """Calculate largest win and largest loss.

        Args:
            fills: List of fills

        Returns:
            Tuple of (largest_win, largest_loss)
        """
        pnls = [float(f.closed_pnl) for f in fills if float(f.closed_pnl) != 0]

        if not pnls:
            return 0.0, 0.0

        largest_win = max(pnls) if any(p > 0 for p in pnls) else 0.0
        largest_loss = min(pnls) if any(p < 0 for p in pnls) else 0.0

        return round(largest_win, 2), round(largest_loss, 2)

    def _calculate_roe(
        self,
        fills: List[Fill],
        state: Any,  # UserState
        days: Optional[int] = None,
    ) -> float:
        """Calculate Return on Equity for a time period.

        Args:
            fills: List of fills
            state: User state with account value
            days: Number of days (None for all time)

        Returns:
            ROE as percentage
        """
        if not fills:
            return 0.0

        # Filter fills by time period
        if days is not None:
            cutoff = datetime.now() - timedelta(days=days)
            period_fills = [f for f in fills if f.datetime >= cutoff]
        else:
            period_fills = fills

        if not period_fills:
            return 0.0

        # Calculate PnL for period
        period_pnl = sum(float(f.closed_pnl) for f in period_fills)

        # Use account value as denominator (approximate)
        account_value = float(state.account_value) if state else 0

        # If no account value, estimate from PnL and position values
        if account_value <= 0:
            # Fallback: use total notional as proxy
            total_notional = sum(float(f.notional_usd) for f in period_fills)
            if total_notional > 0:
                return round((period_pnl / total_notional) * 100, 2)
            return 0.0

        # ROE = PnL / Account Value * 100
        return round((period_pnl / account_value) * 100, 2)


def format_relative_time(timestamp: float) -> str:
    """Format timestamp as relative time (e.g., '5m ago', '2h ago', '3d ago')."""
    if timestamp <= 0:
        return "Never"

    now = time.time()
    diff = now - timestamp

    if diff < 60:
        return f"{int(diff)}s ago"
    elif diff < 3600:
        return f"{int(diff / 60)}m ago"
    elif diff < 86400:
        return f"{int(diff / 3600)}h ago"
    else:
        return f"{int(diff / 86400)}d ago"


def format_age(days: int) -> str:
    """Format account age in human-readable format."""
    if days < 7:
        return f"{days} days"
    elif days < 30:
        weeks = days // 7
        return f"{weeks} week{'s' if weeks > 1 else ''}"
    elif days < 365:
        months = days // 30
        return f"{months} month{'s' if months > 1 else ''}"
    else:
        years = days // 365
        return f"{years} year{'s' if years > 1 else ''}"


def format_roe_indicator(roe: float) -> str:
    """Format ROE with colored indicator."""
    indicator = "🟩" if roe >= 0 else "🟥"
    return f"{roe:.2f}% {indicator}"


def format_drawdown_indicator(drawdown: float) -> str:
    """Format max drawdown with colored indicator."""
    # Lower is better for drawdown
    if drawdown <= 10:
        indicator = "🟩"
    elif drawdown <= 25:
        indicator = "🟨"
    else:
        indicator = "🟥"
    return f"{drawdown:.2f}% {indicator}"


def format_trader_analytics(analytics: TraderAnalytics, bot_username: str = "printdao_bot") -> str:
    """Format analytics for Telegram message in SuperX style.

    Args:
        analytics: TraderAnalytics object
        bot_username: Bot username for deep links

    Returns:
        Formatted HTML string
    """
    address = analytics.address
    addr_short = f"{address[:6]}...{address[-3:]}"

    # Direction bias as percentage (long %)
    long_pct = int((analytics.direction_bias + 1) / 2 * 100)
    bias_text = f"{long_pct}% Long"

    # Last trade relative time
    last_trade = format_relative_time(analytics.last_trade_timestamp)

    # Account age
    age_text = format_age(analytics.account_age_days)

    # Profit factor - handle infinity
    if analytics.profit_factor == float('inf'):
        pf_text = "∞"
    else:
        pf_text = f"{analytics.profit_factor:.2f}"

    lines = [
        f"<a href='https://app.hyperliquid.xyz/explorer/address/{address}'>{addr_short}</a>",
        f"<b>Trader Type:</b> Wallet",
        f"<b>Trader Age:</b> {age_text}",
        f"<b>Profit Factor:</b> {pf_text}",
        f"<b>All Time PNL:</b> ${analytics.all_time_pnl:,.2f}",
        f"<b>7 day ROE:</b> {format_roe_indicator(analytics.roe_7d)}",
        f"<b>30 day ROE:</b> {format_roe_indicator(analytics.roe_30d)}",
        f"<b>90 day ROE:</b> {format_roe_indicator(analytics.roe_90d)}",
        f"<b>All Time ROE:</b> {format_roe_indicator(analytics.roe_all_time)}",
        f"<b>Max Drawdown:</b> {format_drawdown_indicator(analytics.max_drawdown)}",
        f"<b>Trades per day:</b> {analytics.trades_per_day:.2f}",
        f"<b>Direction Bias:</b> {bias_text}",
        f"<b>Last trade:</b> {last_trade}",
        f"<b>Win Rate:</b> {analytics.win_rate:.2f}%",
    ]

    return "\n".join(lines)


def format_trader_analytics_detailed(analytics: TraderAnalytics) -> str:
    """Format detailed analytics with all stats.

    Args:
        analytics: TraderAnalytics object

    Returns:
        Formatted HTML string
    """
    # Trader type emoji
    type_emoji = {
        "scalper": "⚡",
        "day_trader": "📊",
        "swing_trader": "🔄",
        "position_trader": "🏦",
        "new_trader": "🆕",
    }.get(analytics.trader_type, "📈")

    # PnL styling
    pnl_emoji = "🟢" if analytics.all_time_pnl >= 0 else "🔴"
    pnl_sign = "+" if analytics.all_time_pnl >= 0 else ""

    # Direction bias text
    if analytics.direction_bias > 0.3:
        bias_text = "Long-biased"
    elif analytics.direction_bias < -0.3:
        bias_text = "Short-biased"
    else:
        bias_text = "Neutral"

    # Profit factor text
    if analytics.profit_factor >= 2:
        pf_text = f"{analytics.profit_factor:.2f} (Excellent)"
    elif analytics.profit_factor >= 1.5:
        pf_text = f"{analytics.profit_factor:.2f} (Good)"
    elif analytics.profit_factor >= 1:
        pf_text = f"{analytics.profit_factor:.2f} (Profitable)"
    else:
        pf_text = f"{analytics.profit_factor:.2f} (Losing)"

    addr_short = f"{analytics.address[:8]}...{analytics.address[-6:]}"

    lines = [
        f"📊 <b>Trader Analytics</b>",
        f"",
        f"<code>{addr_short}</code>",
        f"",
        f"{type_emoji} <b>Type:</b> {analytics.trader_type.replace('_', ' ').title()}",
        f"📅 <b>Age:</b> {analytics.account_age_days} days",
        f"",
        f"<b>━━━ Performance ━━━</b>",
        f"{pnl_emoji} <b>All-Time PnL:</b> {pnl_sign}${analytics.all_time_pnl:,.2f}",
        f"📈 <b>Profit Factor:</b> {pf_text}",
        f"🎯 <b>Win Rate:</b> {analytics.win_rate:.1f}%",
        f"📉 <b>Max Drawdown:</b> {analytics.max_drawdown:.1f}%",
        f"",
        f"<b>━━━ ROE ━━━</b>",
        f"7d: {analytics.roe_7d:+.2f}% | 30d: {analytics.roe_30d:+.2f}%",
        f"90d: {analytics.roe_90d:+.2f}% | All: {analytics.roe_all_time:+.2f}%",
        f"",
        f"<b>━━━ Activity ━━━</b>",
        f"📊 <b>Total Trades:</b> {analytics.total_trades:,}",
        f"⚡ <b>Trades/Day:</b> {analytics.trades_per_day:.1f}",
        f"💰 <b>Avg Trade:</b> ${analytics.avg_trade_size:,.0f}",
        f"🎯 <b>Bias:</b> {bias_text} ({analytics.direction_bias:+.2f})",
        f"",
        f"<b>━━━ Stats ━━━</b>",
        f"✅ Avg Win: ${analytics.avg_win:,.2f}",
        f"❌ Avg Loss: ${analytics.avg_loss:,.2f}",
        f"🏆 Largest Win: ${analytics.largest_win:,.2f}",
        f"💥 Largest Loss: ${analytics.largest_loss:,.2f}",
    ]

    return "\n".join(lines)
