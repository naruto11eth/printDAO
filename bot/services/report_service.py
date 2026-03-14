"""Scheduled report service — periodic digests of whale activity."""

import json
import logging
import time
from dataclasses import dataclass, asdict
from typing import Any

from bot.storage.redis_client import RedisClient
from shared.formatting import format_usd

logger = logging.getLogger(__name__)

REPORT_SUB_KEY = "report_sub:{user_id}"
VALID_FREQUENCIES = {"4h", "8h", "daily", "weekly"}
FREQUENCY_SECONDS = {"4h": 14400, "8h": 28800, "daily": 86400, "weekly": 604800}


@dataclass
class ReportSubscription:
    user_id: int
    frequency: str = "daily"
    timezone: str = "UTC"
    enabled: bool = True
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReportSubscription":
        from shared.models import safe_from_dict
        return safe_from_dict(cls, data)


class ReportService:
    """Manages scheduled report subscriptions and generates digests."""

    def __init__(self, redis: RedisClient):
        self.redis = redis

    def _key(self, user_id: int) -> str:
        return REPORT_SUB_KEY.format(user_id=user_id)

    async def subscribe(self, user_id: int, frequency: str, timezone: str = "UTC") -> ReportSubscription | str:
        """Subscribe to scheduled reports. Returns sub or error string."""
        if frequency not in VALID_FREQUENCIES:
            return f"Invalid frequency. Use: {', '.join(sorted(VALID_FREQUENCIES))}"

        sub = ReportSubscription(
            user_id=user_id,
            frequency=frequency,
            timezone=timezone,
            created_at=time.time(),
        )
        await self.redis.set(self._key(user_id), sub.to_dict())
        return sub

    async def unsubscribe(self, user_id: int) -> bool:
        """Unsubscribe from reports."""
        return await self.redis.delete(self._key(user_id)) > 0

    async def get_subscription(self, user_id: int) -> ReportSubscription | None:
        """Get user's report subscription."""
        data = await self.redis.get(self._key(user_id))
        if data and isinstance(data, dict):
            return ReportSubscription.from_dict(data)
        return None

    async def get_all_subscriptions(self) -> list[ReportSubscription]:
        """Get all active subscriptions."""
        keys = await self.redis.scan_keys("report_sub:*")
        subs = []
        for key in keys:
            data = await self.redis.get(key)
            if data and isinstance(data, dict):
                sub = ReportSubscription.from_dict(data)
                if sub.enabled:
                    subs.append(sub)
        return subs

    async def generate_digest(self, user_id: int, period_seconds: int) -> str:
        """Generate a digest report for a user covering the given period."""
        lines = ["<b>PrintDAO Report</b>\n"]

        # Whale trades summary
        for prefix, name in [("hl", "Hyperliquid"), ("pm", "Polymarket")]:
            stats = await self.redis.get(f"{prefix}:indexer:stats")
            if stats and isinstance(stats, dict):
                trades = int(stats.get("total_trades_indexed", 0))
                whales = int(stats.get("whale_trades", 0))
                volume = float(stats.get("total_volume_usd", 0))
                lines.append(
                    f"<b>{name}:</b> {trades:,} trades, "
                    f"{whales} whale trades, "
                    f"{format_usd(volume, short=True)} volume"
                )

        # Active whale flows
        flow_keys = await self.redis.scan_keys("whale_flow:*")
        if flow_keys:
            lines.append(f"\n<b>Active Flows:</b> {len(flow_keys)}")

        # Leaderboard top 3
        top_entries = await self.redis.zrevrange_withscores("lb:hyperliquid:score", 0, 2)
        if top_entries:
            lines.append("\n<b>Top Traders:</b>")
            for i, (addr, score) in enumerate(top_entries, 1):
                medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
                addr_short = f"{addr[:6]}...{addr[-4:]}"
                lines.append(f"{medal} <code>{addr_short}</code> — {score:.1f}")

        if len(lines) <= 1:
            lines.append("<i>No activity in this period.</i>")

        return "\n".join(lines)
