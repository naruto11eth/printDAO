"""Copy trading service for generating trade signals from tracked wallets."""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from bot.storage.redis_client import RedisClient
from bot.storage.models import CopyTradeSignal, TrackedWallet
from shared.utils import shorten_address

logger = logging.getLogger(__name__)


@dataclass
class CopyTraderConfig:
    """Configuration for copy trading a specific wallet."""

    address: str
    user_id: int
    enabled: bool = True
    mode: str = "signals"  # "signals" or "auto" (future)
    min_trade_size: float = 1000.0
    max_trade_size: Optional[float] = None
    coins_filter: List[str] = field(default_factory=list)  # Empty = all coins
    exclude_coins: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "address": self.address,
            "user_id": self.user_id,
            "enabled": self.enabled,
            "mode": self.mode,
            "min_trade_size": self.min_trade_size,
            "max_trade_size": self.max_trade_size,
            "coins_filter": self.coins_filter,
            "exclude_coins": self.exclude_coins,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CopyTraderConfig":
        """Create from dictionary."""
        return cls(**data)


class CopyTradingService:
    """Service for managing copy trading signals."""

    def __init__(self, redis: RedisClient):
        self.redis = redis

    async def enable_copy_trading(
        self,
        user_id: int,
        address: str,
        mode: str = "signals",
        min_trade_size: float = 1000.0,
    ) -> CopyTraderConfig:
        """Enable copy trading for a wallet."""
        config = CopyTraderConfig(
            address=address.lower(),
            user_id=user_id,
            enabled=True,
            mode=mode,
            min_trade_size=min_trade_size,
        )

        # Store user's copy trading config
        key = f"copy:{user_id}:{address.lower()}"
        await self.redis.set(key, config.to_dict())

        # Add to wallet's subscriber set for efficient notifications
        subs_key = f"copy:subs:{address.lower()}"
        await self.redis.sadd(subs_key, str(user_id))

        logger.info(f"Enabled copy trading: user={user_id}, wallet={address[:10]}...")
        return config

    async def disable_copy_trading(
        self,
        user_id: int,
        address: str,
    ) -> bool:
        """Disable copy trading for a wallet."""
        key = f"copy:{user_id}:{address.lower()}"
        config_data = await self.redis.get(key)

        if not config_data:
            return False

        config = CopyTraderConfig.from_dict(config_data)
        config.enabled = False
        await self.redis.set(key, config.to_dict())

        # Remove from subscriber set
        subs_key = f"copy:subs:{address.lower()}"
        await self.redis.srem(subs_key, str(user_id))

        logger.info(f"Disabled copy trading: user={user_id}, wallet={address[:10]}...")
        return True

    async def get_copy_config(
        self,
        user_id: int,
        address: str,
    ) -> Optional[CopyTraderConfig]:
        """Get copy trading configuration for a wallet."""
        key = f"copy:{user_id}:{address.lower()}"
        data = await self.redis.get(key)

        if not data:
            return None

        return CopyTraderConfig.from_dict(data)

    async def get_user_copy_wallets(
        self,
        user_id: int,
    ) -> List[CopyTraderConfig]:
        """Get all wallets user is copy trading."""
        pattern = f"copy:{user_id}:*"
        keys = await self.redis.scan_keys(pattern)

        configs = []
        for key in keys:
            data = await self.redis.get(key.decode() if isinstance(key, bytes) else key)
            if data:
                configs.append(CopyTraderConfig.from_dict(data))

        return [c for c in configs if c.enabled]

    async def get_wallet_subscribers(
        self,
        address: str,
    ) -> Set[int]:
        """Get all users subscribed to copy trade a wallet."""
        subs_key = f"copy:subs:{address.lower()}"
        members = await self.redis.smembers(subs_key)

        return {int(m.decode() if isinstance(m, bytes) else m) for m in members}

    async def generate_signal(
        self,
        address: str,
        exchange: str,
        action: str,
        asset: str,
        amount: float,
        price: float,
        notional_usd: float,
        leverage: Optional[int] = None,
        outcome: Optional[str] = None,
        market_title: Optional[str] = None,
        label: Optional[str] = None,
    ) -> Optional[CopyTradeSignal]:
        """Generate a copy trade signal from a detected trade."""
        # Check for duplicate signal
        sig_key = f"copy:dedup:{address}:{asset}:{action}:{notional_usd:.0f}"
        if await self.redis.exists(sig_key):
            return None
        await self.redis.set(sig_key, "1", ex=60)

        signal = CopyTradeSignal(
            source_wallet=address,
            source_label=label,
            action=action,
            exchange=exchange,
            asset=asset,
            amount=amount,
            price=price,
            notional_usd=notional_usd,
            leverage=leverage,
            outcome=outcome,
            market_title=market_title,
        )

        # Store signal in Redis for history
        await self._store_signal(signal)

        return signal

    async def _store_signal(self, signal: CopyTradeSignal) -> None:
        """Store signal in Redis for history."""
        # Store in wallet's signal history
        key = f"copy:signals:{signal.source_wallet.lower()}"
        await self.redis.zadd(
            key,
            {signal.id: signal.timestamp}
        )

        # Store signal details
        detail_key = f"copy:signal:{signal.id}"
        await self.redis.set(detail_key, signal.to_dict(), ex=86400 * 7)  # 7 day TTL

        # Trim to last 100 signals
        await self.redis.zremrangebyrank(key, 0, -101)

    async def get_recent_signals(
        self,
        address: str,
        limit: int = 20,
    ) -> List[CopyTradeSignal]:
        """Get recent signals for a wallet."""
        key = f"copy:signals:{address.lower()}"
        signal_ids = await self.redis.zrange(key, -limit, -1, desc=True)

        signals = []
        for sid in signal_ids:
            sid_str = sid.decode() if isinstance(sid, bytes) else sid
            data = await self.redis.get(f"copy:signal:{sid_str}")
            if data:
                signals.append(CopyTradeSignal(**data))

        return signals

    async def should_send_signal(
        self,
        user_id: int,
        address: str,
        signal: CopyTradeSignal,
    ) -> bool:
        """Check if signal should be sent to user based on their config."""
        config = await self.get_copy_config(user_id, address)

        if not config or not config.enabled:
            return False

        # Check trade size filters
        if signal.notional_usd < config.min_trade_size:
            return False

        if config.max_trade_size and signal.notional_usd > config.max_trade_size:
            return False

        # Check coin filters
        if config.coins_filter and signal.asset not in config.coins_filter:
            return False

        if signal.asset in config.exclude_coins:
            return False

        return True


def format_copy_signal(
    signal: CopyTradeSignal,
    trader_pnl_7d: Optional[float] = None,
) -> str:
    """Format copy trade signal for Telegram."""
    lines = ["<b>COPY TRADE SIGNAL</b>\n"]

    # Trader info
    short_addr = shorten_address(signal.source_wallet)
    trader_label = f" ({signal.source_label})" if signal.source_label else ""

    pnl_str = ""
    if trader_pnl_7d is not None:
        pnl_color = "+" if trader_pnl_7d >= 0 else ""
        pnl_str = f" | 7d PnL: {pnl_color}${trader_pnl_7d:,.0f}"

    lines.append(f"<b>Trader:</b> <code>{short_addr}</code>{trader_label}{pnl_str}")
    lines.append(f"<b>Exchange:</b> {signal.exchange.title()}\n")

    # Trade details
    if signal.market_title:
        lines.append(f"<b>Market:</b> {signal.market_title[:50]}...")

    # Action with emoji
    action_emojis = {
        "LONG": "🟢",
        "SHORT": "🔴",
        "BUY": "🟢",
        "SELL": "🔴",
        "BUY_YES": "🟢",
        "BUY_NO": "🔴",
        "SELL_YES": "🔴",
        "SELL_NO": "🟢",
        "CLOSE_LONG": "🔴",
        "CLOSE_SHORT": "🟢",
        "INCREASE": "🔵",
        "DECREASE": "🟡",
    }

    action_emoji = action_emojis.get(signal.action.upper(), "⚪")
    action_str = signal.action.replace("_", " ")

    if signal.outcome:
        lines.append(f"<b>Action:</b> {action_emoji} {action_str} {signal.outcome}")
    else:
        lines.append(f"<b>Action:</b> {action_emoji} {action_str} {signal.asset}")

    # Size and price
    if signal.leverage:
        lines.append(f"<b>Leverage:</b> {signal.leverage}x")

    lines.append(f"<b>Size:</b> {signal.amount:,.4f} @ ${signal.price:,.2f}")
    lines.append(f"<b>Notional:</b> ${signal.notional_usd:,.2f}")

    # Disclaimer
    lines.append("\n<i>Not financial advice. DYOR.</i>")

    return "\n".join(lines)


def format_signal_history(signals: List[CopyTradeSignal]) -> str:
    """Format signal history for Telegram."""
    if not signals:
        return "No recent signals."

    lines = ["<b>Recent Copy Signals</b>\n"]

    for i, sig in enumerate(signals[:10], 1):
        action_emojis = {
            "LONG": "🟢", "SHORT": "🔴", "BUY": "🟢", "SELL": "🔴",
            "BUY_YES": "🟢", "BUY_NO": "🔴",
        }
        emoji = action_emojis.get(sig.action.upper(), "⚪")

        from datetime import datetime
        time_str = datetime.fromtimestamp(sig.timestamp).strftime("%m/%d %H:%M")

        asset_or_outcome = sig.outcome if sig.outcome else sig.asset
        lines.append(
            f"{i}. {emoji} <b>{sig.action}</b> {asset_or_outcome}\n"
            f"   ${sig.notional_usd:,.0f} @ {time_str}"
        )

    return "\n".join(lines)
