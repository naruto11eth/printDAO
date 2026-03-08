"""Shared constants for PrintDAO."""

import re

from shared.models import Exchange

ETHEREUM_ADDRESS_PATTERN = re.compile(r"^0x[a-fA-F0-9]{40}$")

EXCHANGES = [Exchange.HYPERLIQUID, Exchange.POLYMARKET]

WHALE_THRESHOLDS: dict[str, int] = {
    Exchange.HYPERLIQUID: 10_000_000,
    Exchange.POLYMARKET: 10_000,
}

INTERVAL_LIMITS: dict[str, dict[str, int]] = {
    Exchange.HYPERLIQUID: {
        "min": 30,      # 30 seconds minimum (WebSocket available)
        "max": 3600,    # 1 hour maximum
        "default": 60,  # 1 minute default
    },
    Exchange.POLYMARKET: {
        "min": 60,
        "max": 3600,
        "default": 300,
    },
}

# User limits
MAX_TRACKED_WALLETS_PER_USER = 20
MAX_GROUPS_PER_USER = 10
MAX_WALLETS_PER_GROUP = 50

CACHE_TTL: dict[str, int] = {
    "positions": 60,
    "pnl": 300,
    "whale": 86400 * 30,
    "leaderboard": 300,
}

VALID_PNL_PERIODS = {"day", "week", "month", "all"}
PNL_PERIOD_ALIASES: dict[str, str] = {
    "1d": "day",
    "d": "day",
    "daily": "day",
    "7d": "week",
    "w": "week",
    "weekly": "week",
    "30d": "month",
    "m": "month",
    "monthly": "month",
    "all": "all",
    "overall": "all",
}

REDIS_KEYS = {
    # User data
    "user_wallets": "user:{user_id}:wallets",
    "user_groups": "user:{user_id}:groups",
    "user_preferences": "user:{user_id}:preferences",
    "user_intervals": "user:{user_id}:intervals",
    "wallet_subscribers": "wallet:{address}:subscribers",
    # Whale detector
    "whale_config": "whale_detector:config",
    "whale_profile": "whale_detector:profile:{exchange}:{address}",
    "whale_cooldown": "whale_detector:cooldown:{exchange}:{address}",
    # Copy trading
    "copy_config": "copy:{user_id}:{address}",
    "copy_subscribers": "copy:subs:{address}",
    "copy_signals": "copy:signals:{address}",
    "copy_signal": "copy:signal:{signal_id}",
    # Position indexer
    "position_current": "position:{exchange}:{address}:current",
    "position_history": "position:{exchange}:{address}:history",
    "position_snapshot": "position:{exchange}:{address}:history:{timestamp}",
    "metrics": "metrics:{exchange}:{address}",
    # Analytics
    "analytics_cache": "analytics:{exchange}:{address}",
    # Trade indexer (HL)
    "hl_trades": "hl:trades",
    "hl_trade": "hl:trade:{tid}",
    "hl_whales": "hl:whales",
    "hl_coins": "hl:coins",
    "hl_stats": "hl:indexer:stats",
    # Trade indexer (PM)
    "pm_trades": "pm:trades",
    "pm_trade": "pm:trade:{tx_hash}",
    "pm_whales": "pm:whales",
    "pm_wallets": "pm:wallets",
    "pm_stats": "pm:indexer:stats",
    "pm_last_check": "pm:last_check:{address}",
    "pm_processed": "pm:processed:{tx_hash}",
    "pm_whale_subscribers": "pm:whale_subscribers",
    # Rate limiting
    "rate_limit": "rate:{user_id}:{limit_type}:{window}",
    # Address mapping (for callback data 64-byte limit)
    "addr_map": "addr_map:{short_id}",
    # Caches
    "whale_cache": "{exchange}:whale:{address}",
    "pnl_cache": "{exchange}:pnl:{address}:{period}",
    "position_cache": "{exchange}:positions:{address}",
    # Scheduler
    "monitor_job": "monitor:{user_id}:{exchange}",
}

# Alert types
class AlertType:
    NEW_WHALE_DISCOVERED = "new_whale_discovered"
    LARGE_TRADE = "large_trade"
    TRACKED_WALLET_TRADE = "tracked_wallet_trade"
    PNL_LEADERBOARD = "pnl_leaderboard"

# Trade actions
class TradeAction:
    LONG = "LONG"
    SHORT = "SHORT"
    CLOSE_LONG = "CLOSE_LONG"
    CLOSE_SHORT = "CLOSE_SHORT"
    INCREASE = "INCREASE"
    DECREASE = "DECREASE"
    BUY_YES = "BUY_YES"
    BUY_NO = "BUY_NO"
    SELL_YES = "SELL_YES"
    SELL_NO = "SELL_NO"
