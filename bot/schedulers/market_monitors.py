"""Market monitoring scheduler using APScheduler with Redis job store."""

import asyncio
import logging
import random
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from bot.storage.redis_client import RedisClient
from bot.storage.models import UserPreferences

logger = logging.getLogger(__name__)

# Default interval limits per exchange
INTERVAL_LIMITS = {
    "hyperliquid": {"min": 30, "max": 3600, "default": 60},
    "polymarket": {"min": 60, "max": 3600, "default": 300},
}


class MarketMonitorScheduler:
    """Per-user interval monitoring using APScheduler."""

    def __init__(
        self,
        redis: RedisClient,
        bot: Any = None,
    ):
        self.redis = redis
        self.bot = bot
        self.scheduler = AsyncIOScheduler()
        self._monitor_callbacks: Dict[str, Callable] = {}
        self._running = False

    def register_monitor(
        self,
        exchange: str,
        callback: Callable[[int, str], Coroutine[Any, Any, None]],
    ) -> None:
        """Register a monitoring callback for an exchange.

        Args:
            exchange: Exchange name (e.g., "hyperliquid", "polymarket")
            callback: Async function(user_id, exchange) to call on each interval
        """
        self._monitor_callbacks[exchange] = callback
        logger.info(f"Registered monitor callback for {exchange}")

    async def start(self) -> None:
        """Start the scheduler."""
        if self._running:
            return

        self.scheduler.start()
        self._running = True
        logger.info("Market monitor scheduler started")

        # Schedule whale scanning for all exchanges
        await self._schedule_global_whale_scans()

    async def stop(self) -> None:
        """Stop the scheduler."""
        if not self._running:
            return

        self.scheduler.shutdown(wait=False)
        self._running = False
        logger.info("Market monitor scheduler stopped")

    async def _schedule_global_whale_scans(self) -> None:
        """Schedule global whale scanning tasks."""
        # Hyperliquid: scan every 30 seconds
        self.scheduler.add_job(
            self._global_scan_wrapper,
            trigger=IntervalTrigger(seconds=30),
            id="global:hyperliquid:whale_scan",
            args=["hyperliquid"],
            replace_existing=True,
            jitter=5,
        )

        # Polymarket: scan every 60 seconds
        self.scheduler.add_job(
            self._global_scan_wrapper,
            trigger=IntervalTrigger(seconds=60),
            id="global:polymarket:whale_scan",
            args=["polymarket"],
            replace_existing=True,
            jitter=10,
        )

        logger.info("Scheduled global whale scans")

    async def _global_scan_wrapper(self, exchange: str) -> None:
        """Wrapper for global whale scanning."""
        callback = self._monitor_callbacks.get(f"{exchange}_global")
        if callback:
            try:
                await callback(exchange)
            except Exception as e:
                logger.error(f"Error in global {exchange} scan: {e}")

    async def set_user_interval(
        self,
        user_id: int,
        exchange: str,
        interval_seconds: int,
    ) -> bool:
        """Set monitoring interval for a user+exchange combination.

        Args:
            user_id: Telegram user ID
            exchange: Exchange name
            interval_seconds: Interval in seconds

        Returns:
            True if successful
        """
        # Validate exchange
        limits = INTERVAL_LIMITS.get(exchange)
        if not limits:
            logger.warning(f"Unknown exchange for interval: {exchange}")
            return False

        # Clamp interval to valid range
        interval_seconds = max(
            limits["min"],
            min(limits["max"], interval_seconds)
        )

        job_id = f"monitor:{user_id}:{exchange}"

        # Remove existing job if any
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

        # Add new job with jitter to avoid thundering herd
        jitter = min(5, interval_seconds // 10)

        self.scheduler.add_job(
            self._user_monitor_wrapper,
            trigger=IntervalTrigger(seconds=interval_seconds),
            id=job_id,
            args=[user_id, exchange],
            replace_existing=True,
            jitter=jitter,
        )

        # Store in Redis for persistence
        await self._store_user_interval(user_id, exchange, interval_seconds)

        logger.info(
            f"Set interval: user={user_id}, exchange={exchange}, "
            f"interval={interval_seconds}s"
        )
        return True

    async def remove_user_interval(
        self,
        user_id: int,
        exchange: str,
    ) -> bool:
        """Remove monitoring interval for a user+exchange."""
        job_id = f"monitor:{user_id}:{exchange}"

        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)
            await self._remove_user_interval(user_id, exchange)
            logger.info(f"Removed interval: user={user_id}, exchange={exchange}")
            return True

        return False

    async def pause_user_monitoring(
        self,
        user_id: int,
        exchange: Optional[str] = None,
    ) -> int:
        """Pause monitoring for a user (optionally just one exchange).

        Returns:
            Number of jobs paused
        """
        paused = 0
        prefix = f"monitor:{user_id}:"

        for job in self.scheduler.get_jobs():
            if job.id.startswith(prefix):
                if exchange is None or job.id.endswith(f":{exchange}"):
                    self.scheduler.pause_job(job.id)
                    paused += 1

        logger.info(f"Paused {paused} jobs for user {user_id}")
        return paused

    async def resume_user_monitoring(
        self,
        user_id: int,
        exchange: Optional[str] = None,
    ) -> int:
        """Resume monitoring for a user.

        Returns:
            Number of jobs resumed
        """
        resumed = 0
        prefix = f"monitor:{user_id}:"

        for job in self.scheduler.get_jobs():
            if job.id.startswith(prefix):
                if exchange is None or job.id.endswith(f":{exchange}"):
                    self.scheduler.resume_job(job.id)
                    resumed += 1

        logger.info(f"Resumed {resumed} jobs for user {user_id}")
        return resumed

    async def get_user_jobs(
        self,
        user_id: int,
    ) -> List[Dict[str, Any]]:
        """Get all monitoring jobs for a user."""
        prefix = f"monitor:{user_id}:"
        jobs = []

        for job in self.scheduler.get_jobs():
            if job.id.startswith(prefix):
                exchange = job.id.split(":")[-1]
                jobs.append({
                    "exchange": exchange,
                    "interval": job.trigger.interval.total_seconds(),
                    "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                    "paused": job.next_run_time is None,
                })

        return jobs

    async def _user_monitor_wrapper(
        self,
        user_id: int,
        exchange: str,
    ) -> None:
        """Wrapper to call the appropriate monitor callback."""
        callback = self._monitor_callbacks.get(exchange)
        if not callback:
            logger.warning(f"No callback registered for {exchange}")
            return

        try:
            await callback(user_id, exchange)
        except Exception as e:
            logger.error(f"Error in monitor callback: user={user_id}, exchange={exchange}: {e}")

    async def _store_user_interval(
        self,
        user_id: int,
        exchange: str,
        interval_seconds: int,
    ) -> None:
        """Store user interval in Redis for persistence."""
        key = f"user:{user_id}:intervals"
        await self.redis.redis.hset(key, exchange, str(interval_seconds))

    async def _remove_user_interval(
        self,
        user_id: int,
        exchange: str,
    ) -> None:
        """Remove user interval from Redis."""
        key = f"user:{user_id}:intervals"
        await self.redis.redis.hdel(key, exchange)

    async def restore_user_jobs(self) -> int:
        """Restore all user jobs from Redis on startup.

        Returns:
            Number of jobs restored
        """
        restored = 0

        # Find all user interval keys
        pattern = "user:*:intervals"
        cursor = 0

        while True:
            cursor, keys = await self.redis.redis.scan(cursor, match=pattern, count=100)

            for key in keys:
                key_str = key.decode() if isinstance(key, bytes) else key
                parts = key_str.split(":")
                if len(parts) != 3:
                    continue

                user_id = int(parts[1])
                intervals = await self.redis.redis.hgetall(key_str)

                for exchange, interval in intervals.items():
                    exchange_str = exchange.decode() if isinstance(exchange, bytes) else exchange
                    interval_int = int(interval.decode() if isinstance(interval, bytes) else interval)

                    await self.set_user_interval(user_id, exchange_str, interval_int)
                    restored += 1

            if cursor == 0:
                break

        logger.info(f"Restored {restored} user monitoring jobs")
        return restored


class PositionChangeDetector:
    """Detects position changes for tracked wallets."""

    def __init__(self, redis: RedisClient):
        self.redis = redis

    async def detect_changes(
        self,
        address: str,
        exchange: str,
        current_positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Detect changes between current and previous positions.

        Returns list of changes:
        - new_position: Position opened
        - closed_position: Position closed
        - increased_position: Position size increased
        - decreased_position: Position size decreased
        """
        key = f"positions:{exchange}:{address.lower()}"
        prev_data = await self.redis.get(key)

        # Store current positions
        await self.redis.set(
            key,
            {"positions": current_positions},
            ex=3600  # 1 hour cache
        )

        if not prev_data:
            # First time seeing this wallet - no changes to report
            return []

        prev_positions = {
            p.get("coin", p.get("asset", p.get("market_id"))): p
            for p in prev_data.get("positions", [])
        }

        current_by_asset = {
            p.get("coin", p.get("asset", p.get("market_id"))): p
            for p in current_positions
        }

        changes = []

        # Check for new and changed positions
        for asset, pos in current_by_asset.items():
            prev = prev_positions.get(asset)

            if not prev:
                changes.append({
                    "type": "new_position",
                    "asset": asset,
                    "position": pos,
                })
            else:
                prev_size = abs(float(prev.get("size", prev.get("szi", 0))))
                curr_size = abs(float(pos.get("size", pos.get("szi", 0))))

                if curr_size > prev_size * 1.05:  # 5% threshold
                    changes.append({
                        "type": "increased_position",
                        "asset": asset,
                        "prev_size": prev_size,
                        "curr_size": curr_size,
                        "position": pos,
                    })
                elif curr_size < prev_size * 0.95:  # 5% threshold
                    changes.append({
                        "type": "decreased_position",
                        "asset": asset,
                        "prev_size": prev_size,
                        "curr_size": curr_size,
                        "position": pos,
                    })

        # Check for closed positions
        for asset, prev in prev_positions.items():
            if asset not in current_by_asset:
                changes.append({
                    "type": "closed_position",
                    "asset": asset,
                    "position": prev,
                })

        return changes
