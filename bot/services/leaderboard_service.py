"""Leaderboard service for ranking traders by composite score."""

import logging
import time
from typing import Any

from bot.storage.redis_client import RedisClient
from bot.services.analytics_service import AnalyticsService, TraderAnalytics
from indexers.hyperliquid import HyperliquidClient
from shared.formatting import format_usd

logger = logging.getLogger(__name__)

# Anti-gaming filters
MIN_ACCOUNT_AGE_DAYS = 14
MIN_TRADE_COUNT = 20
MIN_COMPOSITE_SCORE = 1.0


class LeaderboardService:
    """Manages trader leaderboards using Redis sorted sets."""

    LB_KEY = "lb:{exchange}:score"
    TRADER_KEY = "trader:{address}"
    MAX_LEADERBOARD_SIZE = 500

    def __init__(
        self,
        redis: RedisClient,
        analytics_service: AnalyticsService,
        hl_client: HyperliquidClient,
    ):
        self.redis = redis
        self.analytics = analytics_service
        self.hl_client = hl_client

    def _lb_key(self, exchange: str) -> str:
        return self.LB_KEY.format(exchange=exchange)

    def _trader_key(self, address: str) -> str:
        return self.TRADER_KEY.format(address=address.lower())

    async def update_trader(self, address: str, exchange: str = "hyperliquid") -> TraderAnalytics | None:
        """Compute analytics for a trader and update leaderboard."""
        try:
            analytics = await self.analytics.get_trader_analytics(address, exchange, force_refresh=True)
        except Exception as e:
            logger.warning(f"Failed to compute analytics for {address}: {e}")
            return None

        if not self._passes_filters(analytics):
            return analytics

        lb_key = self._lb_key(exchange)
        trader_key = self._trader_key(address)

        await self.redis.zadd(lb_key, {address.lower(): analytics.composite_score})

        trader_data = {
            "address": analytics.address,
            "exchange": exchange,
            "composite_score": str(analytics.composite_score),
            "sortino": str(analytics.sortino_ratio) if analytics.sortino_ratio is not None else "",
            "sharpe": str(analytics.sharpe_ratio) if analytics.sharpe_ratio is not None else "",
            "calmar": str(analytics.calmar_ratio) if analytics.calmar_ratio is not None else "",
            "profit_factor": str(analytics.profit_factor),
            "win_rate": str(analytics.win_rate),
            "all_time_pnl": str(analytics.all_time_pnl),
            "max_drawdown": str(analytics.max_drawdown),
            "total_trades": str(analytics.total_trades),
            "trader_type": analytics.trader_type,
            "account_age_days": str(analytics.account_age_days),
            "updated_at": str(time.time()),
        }
        pipe = self.redis.redis.pipeline(transaction=True)
        pipe.hset(trader_key, mapping=trader_data)
        pipe.expire(trader_key, 86400)
        await pipe.execute()

        return analytics

    def _passes_filters(self, analytics: TraderAnalytics) -> bool:
        """Check if trader passes anti-gaming filters."""
        if analytics.account_age_days < MIN_ACCOUNT_AGE_DAYS:
            return False
        if analytics.total_trades < MIN_TRADE_COUNT:
            return False
        if analytics.composite_score < MIN_COMPOSITE_SCORE:
            return False
        return True

    async def get_leaderboard_page(
        self,
        exchange: str = "hyperliquid",
        page: int = 0,
        page_size: int = 10,
    ) -> list[dict[str, Any]]:
        """Get a page of leaderboard entries."""
        lb_key = self._lb_key(exchange)
        start = page * page_size
        stop = start + page_size - 1

        entries = await self.redis.zrevrange_withscores(lb_key, start, stop)

        results = []
        for address, score in entries:
            trader_key = self._trader_key(address)
            data = await self.redis.hgetall(trader_key)
            if data:
                data["rank"] = start + len(results) + 1
                results.append(data)
            else:
                results.append({
                    "address": address,
                    "composite_score": str(score),
                    "rank": start + len(results) + 1,
                })

        return results

    async def get_trader_rank(self, address: str, exchange: str = "hyperliquid") -> int | None:
        """Get a trader's rank on the leaderboard (1-based)."""
        lb_key = self._lb_key(exchange)
        rank = await self.redis.zrevrank(lb_key, address.lower())
        return rank + 1 if rank is not None else None

    async def get_leaderboard_size(self, exchange: str = "hyperliquid") -> int:
        lb_key = self._lb_key(exchange)
        return await self.redis.zcard(lb_key)

    async def recompute_from_addresses(self, addresses: list[str], exchange: str = "hyperliquid") -> int:
        """Recompute leaderboard for a list of addresses. Returns count updated."""
        updated = 0
        for address in addresses:
            result = await self.update_trader(address, exchange)
            if result and self._passes_filters(result):
                updated += 1
        return updated

    async def prune_leaderboard(self, exchange: str = "hyperliquid") -> int:
        """Remove entries beyond MAX_LEADERBOARD_SIZE."""
        lb_key = self._lb_key(exchange)
        size = await self.redis.zcard(lb_key)
        if size > self.MAX_LEADERBOARD_SIZE:
            removed = await self.redis.zremrangebyrank(lb_key, 0, size - self.MAX_LEADERBOARD_SIZE - 1)
            return removed
        return 0


    async def discover_from_hl_leaderboard(self, limit: int = 100) -> list[str]:
        """Discover top trader addresses from Hyperliquid's leaderboard.

        Fetches the official HL leaderboard, extracts addresses,
        and queues them for analytics computation.
        """
        try:
            def _fetch():
                info = self.hl_client._get_info()
                return info.post("/info", {"type": "leaderboard"})

            loop = __import__("asyncio").get_running_loop()
            from concurrent.futures import ThreadPoolExecutor
            pool = ThreadPoolExecutor(max_workers=1)
            raw = await loop.run_in_executor(pool, _fetch)

            if not raw or not isinstance(raw, list):
                logger.warning("Empty or invalid leaderboard response")
                return []

            addresses = []
            for entry in raw[:limit]:
                if isinstance(entry, dict):
                    addr = entry.get("ethAddress") or entry.get("address", "")
                    if addr and addr.startswith("0x"):
                        addresses.append(addr)

            logger.info(f"Discovered {len(addresses)} addresses from HL leaderboard")
            return addresses

        except Exception as e:
            logger.error(f"Failed to fetch HL leaderboard: {e}")
            return []

    async def run_discovery_cycle(self, max_traders: int = 50) -> int:
        """Run one discovery cycle: fetch HL leaderboard and update scores.

        Returns number of traders updated.
        """
        addresses = await self.discover_from_hl_leaderboard(limit=max_traders)
        if not addresses:
            return 0

        updated = await self.recompute_from_addresses(addresses)
        await self.prune_leaderboard()
        logger.info(f"Discovery cycle complete: {updated}/{len(addresses)} traders updated")
        return updated


def format_leaderboard_page(entries: list[dict], page: int, total: int, page_size: int = 10) -> str:
    """Format a leaderboard page as HTML for Telegram."""
    if not entries:
        return "No traders on the leaderboard yet."

    total_pages = max(1, (total + page_size - 1) // page_size)
    lines = [f"<b>Top Traders — Page {page + 1}/{total_pages}</b>\n"]

    for entry in entries:
        rank = entry.get("rank", "?")
        address = entry.get("address", "???")
        addr_short = f"{address[:6]}...{address[-4:]}" if len(address) > 10 else address
        score = float(entry.get("composite_score", 0))
        pnl = float(entry.get("all_time_pnl", 0))
        win_rate = float(entry.get("win_rate", 0))
        trader_type = entry.get("trader_type", "")

        pnl_str = format_usd(pnl, sign=True, short=True)

        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"{rank}.")

        lines.append(
            f"{medal} <code>{addr_short}</code> — <b>{score:.1f}</b>\n"
            f"    {pnl_str} | WR {win_rate:.0f}% | {trader_type.replace('_', ' ')}"
        )

    return "\n".join(lines)
