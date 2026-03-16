"""Wallet labeling service — persistent labels for tracked addresses."""

import logging
from typing import Any

from bot.storage.redis_client import RedisClient

logger = logging.getLogger(__name__)

VALID_LABEL_TYPES = {"fund", "market_maker", "degen", "kol", "exchange", "smart_money", "scalper", "position_trader", "new_whale", "custom"}
LABEL_KEY = "labels:{address}"


class LabelService:
    """Manages wallet labels in Redis."""

    def __init__(self, redis: RedisClient):
        self.redis = redis

    def _key(self, address: str) -> str:
        return LABEL_KEY.format(address=address.lower())

    async def set_label(self, address: str, label: str, label_type: str = "custom") -> None:
        """Set a label for a wallet address."""
        key = self._key(address)
        await self.redis.hset(key, "label", label)
        await self.redis.hset(key, "type", label_type)

    async def get_label(self, address: str) -> dict[str, str] | None:
        """Get label for a wallet address. Returns {label, type} or None."""
        key = self._key(address)
        data = await self.redis.hgetall(key)
        if data and "label" in data:
            return {"label": data["label"], "type": data.get("type", "custom")}
        return None

    async def remove_label(self, address: str) -> bool:
        """Remove label from a wallet address."""
        key = self._key(address)
        return await self.redis.delete(key) > 0

    async def auto_label_from_analytics(self, address: str, analytics: Any) -> str | None:
        """Automatically assign a behavioral label based on analytics.

        Returns the label assigned, or None if no clear classification.
        """
        existing = await self.get_label(address)
        if existing and existing.get("type") != "auto":
            return None

        label = None
        label_type = "auto"

        if analytics.account_age_days < 7 and analytics.total_trades < 10:
            label = "New Whale"
        elif analytics.trades_per_day > 20:
            label = "Scalper"
        elif analytics.trades_per_day < 1 and analytics.avg_trade_size > 100_000:
            label = "Position Trader"
        elif analytics.composite_score >= 70 and analytics.total_trades >= 50:
            label = "Smart Money"

        if label:
            await self.set_label(address, label, label_type)

        return label

    async def get_display_label(self, address: str) -> str:
        """Get a display-ready label string. Returns empty string if no label."""
        data = await self.get_label(address)
        if data:
            return data["label"]
        return ""
