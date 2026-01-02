"""Redis client for PrintDAO storage."""

import json
import logging
from typing import Any, Dict, List, Optional, Set

import redis.asyncio as redis
from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class RedisClient:
    """Async Redis client wrapper for PrintDAO."""

    def __init__(self, redis_url: str):
        """Initialize Redis client.

        Args:
            redis_url: Redis connection URL (e.g., redis://localhost:6379/0)
        """
        self.redis_url = redis_url
        self._redis: Optional[Redis] = None

    async def connect(self) -> None:
        """Establish connection to Redis."""
        if self._redis is None:
            self._redis = redis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
            # Test connection
            await self._redis.ping()
            logger.info("Connected to Redis")

    async def disconnect(self) -> None:
        """Close Redis connection."""
        if self._redis:
            await self._redis.close()
            self._redis = None
            logger.info("Disconnected from Redis")

    @property
    def redis(self) -> Redis:
        """Get Redis instance, raising if not connected."""
        if self._redis is None:
            raise RuntimeError("Redis not connected. Call connect() first.")
        return self._redis

    # --- Hash Operations (for user wallets, groups) ---
