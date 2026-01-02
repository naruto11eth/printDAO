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

    async def hset(self, key: str, field: str, value: Any) -> None:
        """Set a hash field to a value (JSON serialized)."""
        serialized = json.dumps(value) if not isinstance(value, str) else value
        await self.redis.hset(key, field, serialized)

    async def hget(self, key: str, field: str) -> Optional[Any]:
        """Get a hash field value (JSON deserialized)."""
        value = await self.redis.hget(key, field)
        if value is None:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    async def hgetall(self, key: str) -> Dict[str, Any]:
        """Get all fields and values in a hash."""
        data = await self.redis.hgetall(key)
        result = {}
        for field, value in data.items():
            try:
                result[field] = json.loads(value)
            except json.JSONDecodeError:
                result[field] = value
        return result

    async def hdel(self, key: str, *fields: str) -> int:
        """Delete one or more hash fields."""
        return await self.redis.hdel(key, *fields)

    async def hlen(self, key: str) -> int:
        """Get the number of fields in a hash."""
        return await self.redis.hlen(key)

    async def hexists(self, key: str, field: str) -> bool:
        """Check if a hash field exists."""
        return await self.redis.hexists(key, field)

    # --- Set Operations (for wallet subscribers) ---
