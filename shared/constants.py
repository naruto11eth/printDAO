"""Shared constants for PrintDAO."""

import re
from typing import Dict, Any

# Wallet address validation
ETHEREUM_ADDRESS_PATTERN = re.compile(r"^0x[a-fA-F0-9]{40}$")

# Supported exchanges
EXCHANGES = ["hyperliquid", "polymarket"]

# Whale thresholds (in USD)
WHALE_THRESHOLDS: Dict[str, int] = {
    "hyperliquid": 10_000_000,  # $10M for perps
    "polymarket": 10_000,       # $10k for prediction markets
}

# Interval limits (in seconds)
INTERVAL_LIMITS: Dict[str, Dict[str, int]] = {
    "hyperliquid": {
        "min": 30,      # 30 seconds minimum (WebSocket available)
        "max": 3600,    # 1 hour maximum
        "default": 60,  # 1 minute default
    },
    "polymarket": {
        "min": 60,      # 1 minute minimum (API rate limits)
        "max": 3600,    # 1 hour maximum
        "default": 300, # 5 minutes default
    },
}

# User limits
MAX_TRACKED_WALLETS_PER_USER = 20
MAX_GROUPS_PER_USER = 10
MAX_WALLETS_PER_GROUP = 50

# Cache TTLs (in seconds)
CACHE_TTL: Dict[str, int] = {
    "positions": 60,       # 1 minute
    "pnl": 300,            # 5 minutes
    "whale": 86400 * 30,   # 30 days
    "leaderboard": 300,    # 5 minutes
}

# PnL periods
VALID_PNL_PERIODS = {"day", "week", "month", "all"}
PNL_PERIOD_ALIASES: Dict[str, str] = {
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

# Redis key patterns
REDIS_KEYS = {
    "user_wallets": "user:{user_id}:wallets",
    "user_groups": "user:{user_id}:groups",
    "wallet_subscribers": "wallet:{address}:subscribers",
    "whale_cache": "{exchange}:whale:{address}",
    "pnl_cache": "{exchange}:pnl:{address}:{period}",
    "position_cache": "{exchange}:positions:{address}",
}

# Alert types