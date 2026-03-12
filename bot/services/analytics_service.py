"""Trader analytics service with risk-adjusted metrics and composite scoring."""

import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Any

from indexers.hyperliquid import HyperliquidClient, Fill
from bot.storage.redis_client import RedisClient
from shared.models import safe_from_dict

logger = logging.getLogger(__name__)


@dataclass
class TraderAnalytics:
    """Comprehensive trader analytics with risk metrics."""

    address: str
    exchange: str

    # Classification
    trader_type: str = "new_trader"
    account_age_days: int = 0

    # Performance
    profit_factor: float = 0.0
    all_time_pnl: float = 0.0

    # ROE by timeframe
    roe_7d: float = 0.0
    roe_30d: float = 0.0
    roe_90d: float = 0.0
    roe_all_time: float = 0.0

    # Risk metrics (new)
    max_drawdown: float = 0.0
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    calmar_ratio: float | None = None
    composite_score: float = 0.0

    # Activity
    total_trades: int = 0
    trades_per_day: float = 0.0
    avg_trade_size: float = 0.0
    direction_bias: float = 0.0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    last_trade_timestamp: float = 0.0

    # Metadata
    calculated_at: float = field(default_factory=time.time)
    fills_analyzed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TraderAnalytics":
        return safe_from_dict(cls, data)


@dataclass
class _MetricsAccumulator:
    """Single-pass accumulator for all trader metrics.

    Processes fills in chronological order (oldest first).
    Computes everything in one iteration over the fills list.
    """
    total_trades: int = 0
    long_count: int = 0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    win_count: int = 0
    loss_count: int = 0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    total_notional: float = 0.0
    first_trade_ms: int = 0
    last_trade_ms: int = 0

    # Equity curve for drawdown
    cumulative_pnl: float = 0.0
    peak_equity: float = 0.0
    max_drawdown_pct: float = 0.0

    # Daily PnL buckets for Sharpe/Sortino
    _daily_pnl: dict = field(default_factory=lambda: defaultdict(float))
    _daily_notional: dict = field(default_factory=lambda: defaultdict(float))

    def ingest(self, pnl: float, notional: float, is_long: bool, time_ms: int) -> None:
        self.total_trades += 1
        self.total_notional += notional

        if is_long:
            self.long_count += 1

        if self.first_trade_ms == 0:
            self.first_trade_ms = time_ms
        self.last_trade_ms = time_ms

        if pnl > 0:
            self.gross_profit += pnl
            self.win_count += 1
            self.largest_win = max(self.largest_win, pnl)
        elif pnl < 0:
            self.gross_loss += abs(pnl)
            self.loss_count += 1
            self.largest_loss = min(self.largest_loss, pnl)

        # Equity curve
        self.cumulative_pnl += pnl
        if self.cumulative_pnl > self.peak_equity:
            self.peak_equity = self.cumulative_pnl
        if self.peak_equity > 0:
            dd = (self.peak_equity - self.cumulative_pnl) / self.peak_equity * 100
            self.max_drawdown_pct = max(self.max_drawdown_pct, dd)

        # Daily buckets for risk ratios
        day_key = datetime.fromtimestamp(time_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        self._daily_pnl[day_key] += pnl
        self._daily_notional[day_key] += notional

    @property
    def profit_factor(self) -> float:
        if self.gross_loss == 0:
            return min(5.0, self.gross_profit) if self.gross_profit > 0 else 0.0
        return min(self.gross_profit / self.gross_loss, 5.0)

    @property
    def win_rate(self) -> float:
        pnl_trades = self.win_count + self.loss_count
        return (self.win_count / pnl_trades * 100) if pnl_trades > 0 else 0.0

    @property
    def direction_bias(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return (self.long_count - (self.total_trades - self.long_count)) / self.total_trades

    @property
    def avg_trade_size(self) -> float:
        return self.total_notional / self.total_trades if self.total_trades > 0 else 0.0

    @property
    def avg_win(self) -> float:
        return self.gross_profit / self.win_count if self.win_count > 0 else 0.0

    @property
    def avg_loss(self) -> float:
        return -(self.gross_loss / self.loss_count) if self.loss_count > 0 else 0.0

    @property
    def account_age_days(self) -> int:
        if self.first_trade_ms == 0:
            return 0
        first = datetime.fromtimestamp(self.first_trade_ms / 1000, tz=timezone.utc)
        age = datetime.now(tz=timezone.utc) - first
        return max(1, age.days)

    @property
    def trades_per_day(self) -> float:
        days = self.account_age_days
        return self.total_trades / days if days > 0 else 0.0

    def _daily_returns(self, equity_start: float) -> list[float]:
        """Compute daily returns as PnL / rolling equity."""
        if not self._daily_pnl or equity_start <= 0:
            return []
        sorted_days = sorted(self._daily_pnl.keys())
        returns = []
        equity = equity_start
        for day in sorted_days:
            if equity <= 0:
                break
            day_return = self._daily_pnl[day] / equity
            returns.append(day_return)
            equity += self._daily_pnl[day]
        return returns

    def sharpe(self, equity_start: float) -> float | None:
        """Annualized Sharpe ratio. Risk-free rate = 0%."""
        returns = self._daily_returns(equity_start)
        if len(returns) < 30:
            return None
        mean_r = sum(returns) / len(returns)
        variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        std = math.sqrt(variance)
        if std == 0:
            return None
        return min(mean_r / std * math.sqrt(365), 5.0)

    def sortino(self, equity_start: float) -> float | None:
        """Annualized Sortino ratio. Only penalizes downside volatility."""
        returns = self._daily_returns(equity_start)
        if len(returns) < 30:
            return None
        mean_r = sum(returns) / len(returns)
        downside = [min(r, 0.0) for r in returns]
        downside_dev = math.sqrt(sum(d ** 2 for d in downside) / len(downside))
        if downside_dev == 0:
            return None
        return min(mean_r / downside_dev * math.sqrt(365), 5.0)

    def calmar(self, equity_start: float) -> float | None:
        """Calmar ratio = annualized return / max drawdown."""
        returns = self._daily_returns(equity_start)
        if len(returns) < 90 or self.max_drawdown_pct == 0:
            return None
        annualized_return = (sum(returns) / len(returns)) * 365 * 100
        return min(annualized_return / self.max_drawdown_pct, 5.0)


def compute_composite_score(
    *,
    sortino: float | None,
    max_drawdown_pct: float,
    profit_factor: float,
    win_rate_pct: float,
    trade_count: int,
) -> float:
    """Compute normalized composite score in [0, 100].

    All ratio inputs are capped to prevent inf/outlier domination.
    A confidence multiplier penalizes low sample sizes.
    """
    s = sortino if sortino is not None else 0.0
    norm_sortino = min(max(s, -2.0), 5.0) / 5.0
    norm_drawdown = 1.0 - min(max_drawdown_pct, 100.0) / 100.0
    capped_pf = min(profit_factor, 5.0) if math.isfinite(profit_factor) else 5.0
    norm_pf = capped_pf / 5.0
    norm_wr = min(win_rate_pct, 100.0) / 100.0

    raw = (
        0.30 * norm_sortino
        + 0.25 * norm_drawdown
        + 0.25 * norm_pf
        + 0.20 * norm_wr
    )

    confidence = min(trade_count / 100.0, 1.0)
    return round(raw * confidence * 100, 2)


class AnalyticsService:
    """Service for calculating trader analytics."""

    CACHE_KEY_PREFIX = "analytics"

    def __init__(
        self,
        hl_client: HyperliquidClient,
        redis: RedisClient | None = None,
        cache_ttl: int = 300,
    ):
        self.hl_client = hl_client
        self.redis = redis
        self.cache_ttl = cache_ttl

    async def get_trader_analytics(
        self,
        address: str,
        exchange: str = "hyperliquid",
        force_refresh: bool = False,
    ) -> TraderAnalytics:
        cache_key = f"{self.CACHE_KEY_PREFIX}:{exchange}:{address.lower()}"

        if not force_refresh and self.redis:
            cached = await self.redis.get(cache_key)
            if cached:
                try:
                    return TraderAnalytics.from_dict(cached)
                except Exception as e:
                    logger.warning(f"Failed to deserialize cached analytics: {e}")

        analytics = await self._calculate_analytics(address, exchange)

        if self.redis:
            try:
                await self.redis.set(cache_key, analytics.to_dict(), ex=self.cache_ttl)
            except Exception as e:
                logger.warning(f"Failed to cache analytics: {e}")

        return analytics

    async def _calculate_analytics(self, address: str, exchange: str) -> TraderAnalytics:
        """Calculate all analytics in a single pass over fills."""
        try:
            fills = await self.hl_client.get_user_fills(address, limit=2000)
            state = await self.hl_client.get_user_state(address)
        except Exception as e:
            logger.error(f"Failed to fetch data for {address}: {e}")
            return TraderAnalytics(address=address, exchange=exchange)

        if not fills:
            return TraderAnalytics(address=address, exchange=exchange)

        acc = _MetricsAccumulator()
        for fill in sorted(fills, key=lambda f: f.time_ms):
            acc.ingest(
                pnl=float(fill.closed_pnl),
                notional=float(fill.notional_usd),
                is_long=fill.side.value == "B",
                time_ms=fill.time_ms,
            )

        account_value = float(state.account_value) if state else 1.0
        # Estimate starting equity by subtracting cumulative PnL
        equity_start = max(account_value - acc.cumulative_pnl, 1.0)

        sharpe = acc.sharpe(equity_start)
        sortino = acc.sortino(equity_start)
        calmar = acc.calmar(equity_start)

        score = compute_composite_score(
            sortino=sortino,
            max_drawdown_pct=acc.max_drawdown_pct,
            profit_factor=acc.profit_factor,
            win_rate_pct=acc.win_rate,
            trade_count=acc.total_trades,
        )

        # ROE calculations using portfolio endpoint when available
        roe_7d = self._calculate_roe_from_fills(fills, account_value, days=7)
        roe_30d = self._calculate_roe_from_fills(fills, account_value, days=30)
        roe_90d = self._calculate_roe_from_fills(fills, account_value, days=90)
        roe_all = self._calculate_roe_from_fills(fills, account_value, days=None)

        trader_type = self._classify_trader_type(acc.trades_per_day, acc.account_age_days)

        return TraderAnalytics(
            address=address,
            exchange=exchange,
            trader_type=trader_type,
            account_age_days=acc.account_age_days,
            profit_factor=acc.profit_factor,
            all_time_pnl=acc.cumulative_pnl,
            roe_7d=roe_7d,
            roe_30d=roe_30d,
            roe_90d=roe_90d,
            roe_all_time=roe_all,
            max_drawdown=acc.max_drawdown_pct,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            calmar_ratio=calmar,
            composite_score=score,
            total_trades=acc.total_trades,
            trades_per_day=acc.trades_per_day,
            avg_trade_size=acc.avg_trade_size,
            direction_bias=acc.direction_bias,
            win_rate=acc.win_rate,
            avg_win=acc.avg_win,
            avg_loss=acc.avg_loss,
            largest_win=acc.largest_win,
            largest_loss=acc.largest_loss,
            last_trade_timestamp=acc.last_trade_ms / 1000 if acc.last_trade_ms else 0,
            fills_analyzed=len(fills),
        )

    def _calculate_roe_from_fills(
        self, fills: list[Fill], account_value: float, days: int | None,
    ) -> float:
        if not fills or account_value <= 0:
            return 0.0
        if days is not None:
            cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
            period_fills = [f for f in fills if f.datetime >= cutoff]
        else:
            period_fills = fills
        if not period_fills:
            return 0.0
        period_pnl = sum(float(f.closed_pnl) for f in period_fills)
        return round((period_pnl / account_value) * 100, 2)

    @staticmethod
    def _classify_trader_type(trades_per_day: float, account_age_days: int) -> str:
        if account_age_days < 7:
            return "new_trader"
        if trades_per_day > 20:
            return "scalper"
        if trades_per_day > 5:
            return "day_trader"
        if trades_per_day > 1:
            return "swing_trader"
        return "position_trader"


# --- Formatting functions ---

def format_relative_time(timestamp: float) -> str:
    if timestamp <= 0:
        return "Never"
    diff = time.time() - timestamp
    if diff < 60:
        return f"{int(diff)}s ago"
    elif diff < 3600:
        return f"{int(diff / 60)}m ago"
    elif diff < 86400:
        return f"{int(diff / 3600)}h ago"
    return f"{int(diff / 86400)}d ago"


def format_age(days: int) -> str:
    if days < 7:
        return f"{days} days"
    elif days < 30:
        return f"{days // 7} week{'s' if days // 7 > 1 else ''}"
    elif days < 365:
        return f"{days // 30} month{'s' if days // 30 > 1 else ''}"
    return f"{days // 365} year{'s' if days // 365 > 1 else ''}"


def _format_ratio(value: float | None, cap: float = 5.0) -> str:
    if value is None:
        return "N/A"
    return f"{value:.2f}"


def format_trader_analytics(analytics: TraderAnalytics, bot_username: str = "printdao_bot") -> str:
    """Format analytics for Telegram message with risk metrics."""
    address = analytics.address
    addr_short = f"{address[:6]}...{address[-3:]}"
    age_text = format_age(analytics.account_age_days)
    last_trade = format_relative_time(analytics.last_trade_timestamp)

    pf_text = f"{analytics.profit_factor:.2f}" if analytics.profit_factor < 5.0 else "5.00+"

    long_pct = int((analytics.direction_bias + 1) / 2 * 100)

    def roe_indicator(roe: float) -> str:
        indicator = "🟩" if roe >= 0 else "🟥"
        return f"{roe:.2f}% {indicator}"

    def dd_indicator(dd: float) -> str:
        if dd <= 10:
            return f"{dd:.1f}% 🟩"
        elif dd <= 25:
            return f"{dd:.1f}% 🟨"
        return f"{dd:.1f}% 🟥"

    lines = [
        f"<a href='https://app.hyperliquid.xyz/explorer/address/{address}'>{addr_short}</a>",
        f"<b>Trader Type:</b> {analytics.trader_type.replace('_', ' ').title()}",
        f"<b>Trader Age:</b> {age_text}",
        "",
        f"<b>Profit Factor:</b> {pf_text}",
        f"<b>All Time PNL:</b> ${analytics.all_time_pnl:,.2f}",
        f"<b>Composite Score:</b> {analytics.composite_score:.1f}/100",
        "",
        f"<b>Sortino:</b> {_format_ratio(analytics.sortino_ratio)}",
        f"<b>Sharpe:</b> {_format_ratio(analytics.sharpe_ratio)}",
        f"<b>Calmar:</b> {_format_ratio(analytics.calmar_ratio)}",
        "",
        f"<b>7d ROE:</b> {roe_indicator(analytics.roe_7d)}",
        f"<b>30d ROE:</b> {roe_indicator(analytics.roe_30d)}",
        f"<b>90d ROE:</b> {roe_indicator(analytics.roe_90d)}",
        f"<b>All Time ROE:</b> {roe_indicator(analytics.roe_all_time)}",
        "",
        f"<b>Max Drawdown:</b> {dd_indicator(analytics.max_drawdown)}",
        f"<b>Win Rate:</b> {analytics.win_rate:.1f}%",
        f"<b>Trades/day:</b> {analytics.trades_per_day:.1f}",
        f"<b>Direction:</b> {long_pct}% Long",
        f"<b>Last trade:</b> {last_trade}",
    ]

    if analytics.fills_analyzed >= 2000:
        lines.append(f"\n<i>Based on last {analytics.fills_analyzed} fills (may be truncated)</i>")

    return "\n".join(lines)


def format_trader_analytics_detailed(analytics: TraderAnalytics) -> str:
    """Format detailed analytics with all stats."""
    type_emoji = {
        "scalper": "⚡", "day_trader": "📊", "swing_trader": "🔄",
        "position_trader": "🏦", "new_trader": "🆕",
    }.get(analytics.trader_type, "📈")

    pnl_emoji = "🟢" if analytics.all_time_pnl >= 0 else "🔴"
    addr_short = f"{analytics.address[:8]}...{analytics.address[-6:]}"

    lines = [
        f"📊 <b>Trader Analytics</b>",
        f"<code>{addr_short}</code>",
        f"",
        f"{type_emoji} <b>Type:</b> {analytics.trader_type.replace('_', ' ').title()}",
        f"📅 <b>Age:</b> {analytics.account_age_days} days",
        f"🏆 <b>Score:</b> {analytics.composite_score:.1f}/100",
        f"",
        f"<b>━━━ Performance ━━━</b>",
        f"{pnl_emoji} <b>PnL:</b> ${analytics.all_time_pnl:,.2f}",
        f"📈 <b>Profit Factor:</b> {analytics.profit_factor:.2f}",
        f"🎯 <b>Win Rate:</b> {analytics.win_rate:.1f}%",
        f"📉 <b>Max Drawdown:</b> {analytics.max_drawdown:.1f}%",
        f"",
        f"<b>━━━ Risk Metrics ━━━</b>",
        f"📊 <b>Sortino:</b> {_format_ratio(analytics.sortino_ratio)}",
        f"📊 <b>Sharpe:</b> {_format_ratio(analytics.sharpe_ratio)}",
        f"📊 <b>Calmar:</b> {_format_ratio(analytics.calmar_ratio)}",
        f"",
        f"<b>━━━ ROE ━━━</b>",
        f"7d: {analytics.roe_7d:+.2f}% | 30d: {analytics.roe_30d:+.2f}%",
        f"90d: {analytics.roe_90d:+.2f}% | All: {analytics.roe_all_time:+.2f}%",
        f"",
        f"<b>━━━ Activity ━━━</b>",
        f"📊 <b>Trades:</b> {analytics.total_trades:,} ({analytics.trades_per_day:.1f}/day)",
        f"💰 <b>Avg Trade:</b> ${analytics.avg_trade_size:,.0f}",
        f"✅ <b>Avg Win:</b> ${analytics.avg_win:,.2f}",
        f"❌ <b>Avg Loss:</b> ${analytics.avg_loss:,.2f}",
        f"🏆 <b>Best:</b> ${analytics.largest_win:,.2f}",
        f"💥 <b>Worst:</b> ${analytics.largest_loss:,.2f}",
    ]

    return "\n".join(lines)
