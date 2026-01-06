"""Rate limiting middleware for Telegram bot."""

import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, TelegramObject

from bot.storage.redis_client import RedisClient

logger = logging.getLogger(__name__)


class RateLimitConfig:
    """Rate limit configuration."""

    def __init__(
        self,
        requests_per_minute: int = 20,
        requests_per_hour: int = 200,
        burst_limit: int = 5,
        burst_window: int = 10,
        cooldown_seconds: int = 60,
    ):
        self.requests_per_minute = requests_per_minute
        self.requests_per_hour = requests_per_hour
        self.burst_limit = burst_limit
        self.burst_window = burst_window
        self.cooldown_seconds = cooldown_seconds


# Different limits for different command types
RATE_LIMITS = {
    "default": RateLimitConfig(
        requests_per_minute=20,
        requests_per_hour=200,
        burst_limit=5,
        burst_window=10,
    ),
    "expensive": RateLimitConfig(
        requests_per_minute=5,
        requests_per_hour=30,
        burst_limit=2,
        burst_window=30,
    ),
    "analytics": RateLimitConfig(
        requests_per_minute=10,
        requests_per_hour=60,
        burst_limit=3,
        burst_window=15,
    ),
}

# Map commands to rate limit types
COMMAND_LIMITS = {
    # Expensive commands (API-heavy)
    "hl_analytics": "expensive",
    "hl_positions": "analytics",
    "hl_pnl": "analytics",
    "hl_fills": "expensive",
    "pm_positions": "analytics",
    "pm_pnl": "analytics",
    "pm_trades": "analytics",
    "pm_whales": "default",
    "pm_leaderboard": "expensive",
    # Standard commands use default limits
}


class RateLimiter:
    """Token bucket rate limiter with Redis storage."""

    def __init__(self, redis: RedisClient, config: RateLimitConfig):
        self.redis = redis
        self.config = config

    async def is_rate_limited(
        self,
        user_id: int,
        limit_type: str = "default",
    ) -> tuple[bool, Optional[int]]:
        """Check if user is rate limited.

        Returns:
            (is_limited, retry_after_seconds)
        """
        now = time.time()
        minute_key = f"rate:{user_id}:{limit_type}:minute"
        hour_key = f"rate:{user_id}:{limit_type}:hour"
        burst_key = f"rate:{user_id}:{limit_type}:burst"

        config = RATE_LIMITS.get(limit_type, RATE_LIMITS["default"])

        # Check burst limit
        burst_count = await self._get_window_count(burst_key, config.burst_window)
        if burst_count >= config.burst_limit:
            return True, config.burst_window

        # Check per-minute limit
        minute_count = await self._get_window_count(minute_key, 60)
        if minute_count >= config.requests_per_minute:
            return True, 60

        # Check per-hour limit
        hour_count = await self._get_window_count(hour_key, 3600)
        if hour_count >= config.requests_per_hour:
            return True, 3600

        # Not rate limited - record the request
        await self._record_request(user_id, limit_type)

        return False, None

    async def _get_window_count(self, key: str, window_seconds: int) -> int:
        """Get request count in sliding window."""
        now = time.time()
        window_start = now - window_seconds

        # Remove old entries and count current
        await self.redis.redis.zremrangebyscore(key, 0, window_start)
        count = await self.redis.redis.zcard(key)

        return count

    async def _record_request(
        self,
        user_id: int,
        limit_type: str,
    ) -> None:
        """Record a request for rate limiting."""
        now = time.time()

        for window_name, window_seconds in [("burst", 10), ("minute", 60), ("hour", 3600)]:
            key = f"rate:{user_id}:{limit_type}:{window_name}"

            # Add current request
            await self.redis.redis.zadd(key, {str(now): now})

            # Set expiry slightly longer than window
            await self.redis.redis.expire(key, window_seconds + 10)

    async def get_usage(
        self,
        user_id: int,
        limit_type: str = "default",
    ) -> Dict[str, Any]:
        """Get current rate limit usage for a user."""
        config = RATE_LIMITS.get(limit_type, RATE_LIMITS["default"])

        burst_key = f"rate:{user_id}:{limit_type}:burst"
        minute_key = f"rate:{user_id}:{limit_type}:minute"
        hour_key = f"rate:{user_id}:{limit_type}:hour"

        return {
            "burst": {
                "used": await self._get_window_count(burst_key, config.burst_window),
                "limit": config.burst_limit,
                "window": config.burst_window,
            },
            "minute": {
                "used": await self._get_window_count(minute_key, 60),
                "limit": config.requests_per_minute,
            },
            "hour": {
                "used": await self._get_window_count(hour_key, 3600),
                "limit": config.requests_per_hour,
            },
        }


class RateLimitMiddleware(BaseMiddleware):
    """Middleware to apply rate limiting to bot commands."""

    def __init__(self, redis: RedisClient):
        self.redis = redis
        self.rate_limiter = RateLimiter(redis, RATE_LIMITS["default"])
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        """Apply rate limiting before handler execution."""
        user_id = None
        limit_type = "default"

        # Get user ID based on event type
        if isinstance(event, Message):
            user_id = event.from_user.id if event.from_user else None

            # Determine limit type from command
            if event.text and event.text.startswith("/"):
                command = event.text.split()[0][1:].split("@")[0]
                limit_type = COMMAND_LIMITS.get(command, "default")

        elif isinstance(event, CallbackQuery):
            user_id = event.from_user.id if event.from_user else None
            # Callbacks use default limits

        if user_id is None:
            return await handler(event, data)

        # Check rate limit
        is_limited, retry_after = await self.rate_limiter.is_rate_limited(
            user_id, limit_type
        )

        if is_limited:
            logger.warning(
                f"Rate limited: user={user_id}, type={limit_type}, retry_after={retry_after}"
            )

            # Send rate limit message
            if isinstance(event, Message):
                await event.reply(
                    f"You're making requests too quickly. "
                    f"Please wait {retry_after} seconds before trying again."
                )
            elif isinstance(event, CallbackQuery):
                await event.answer(
                    f"Too many requests. Wait {retry_after}s.",
                    show_alert=True
                )

            return None

        # Add rate limiter to data for handlers to use
        data["rate_limiter"] = self.rate_limiter

        return await handler(event, data)


class ThrottleMiddleware(BaseMiddleware):
    """Simple throttle middleware for flood control."""

    def __init__(self, limit: float = 0.5):
        """
        Args:
            limit: Minimum seconds between messages from same user
        """
        self.limit = limit
        self._last_request: Dict[int, float] = {}
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        """Apply simple throttling."""
        user_id = None

        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.from_user:
            user_id = event.from_user.id

        if user_id is None:
            return await handler(event, data)

        now = time.time()
        last = self._last_request.get(user_id, 0)

        if now - last < self.limit:
            # Silently drop - don't even acknowledge
            return None

        self._last_request[user_id] = now

        # Cleanup old entries periodically
        if len(self._last_request) > 1000:
            cutoff = now - 60
            self._last_request = {
                k: v for k, v in self._last_request.items() if v > cutoff
            }

        return await handler(event, data)


def format_rate_limit_status(usage: Dict[str, Any]) -> str:
    """Format rate limit usage for display."""
    lines = ["<b>Rate Limit Status</b>\n"]

    for window, data in usage.items():
        used = data["used"]
        limit = data["limit"]
        pct = (used / limit * 100) if limit > 0 else 0

        bar_len = 10
        filled = int(bar_len * pct / 100)
        bar = "" * filled + "" * (bar_len - filled)

        lines.append(
            f"<b>{window.title()}:</b> {used}/{limit}\n"
            f"  [{bar}] {pct:.0f}%"
        )

    return "\n".join(lines)
