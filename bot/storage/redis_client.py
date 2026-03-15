"""Redis client for PrintDAO storage."""

import json
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from typing import Any

import redis.asyncio as aioredis
from redis.asyncio import Redis
from redis.asyncio.client import Pipeline

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
            self._redis = aioredis.from_url(
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

    async def sadd(self, key: str, *members: str) -> int:
        """Add one or more members to a set."""
        return await self.redis.sadd(key, *members)

    async def srem(self, key: str, *members: str) -> int:
        """Remove one or more members from a set."""
        return await self.redis.srem(key, *members)

    async def smembers(self, key: str) -> Set[str]:
        """Get all members of a set."""
        return await self.redis.smembers(key)

    async def sismember(self, key: str, member: str) -> bool:
        """Check if member is in set."""
        return await self.redis.sismember(key, member)

    async def scard(self, key: str) -> int:
        """Get the number of members in a set."""
        return await self.redis.scard(key)

    # --- String/JSON Operations (for caches) ---

    async def set(
        self,
        key: str,
        value: Any,
        ex: Optional[int] = None,
    ) -> None:
        """Set a key to a value with optional expiration."""
        serialized = json.dumps(value) if not isinstance(value, str) else value
        await self.redis.set(key, serialized, ex=ex)

    async def get(self, key: str) -> Optional[Any]:
        """Get a key value."""
        value = await self.redis.get(key)
        if value is None:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    async def delete(self, *keys: str) -> int:
        """Delete one or more keys."""
        return await self.redis.delete(*keys)

    async def exists(self, key: str) -> bool:
        """Check if a key exists."""
        return await self.redis.exists(key) > 0

    async def ttl(self, key: str) -> int:
        """Get time-to-live for a key in seconds."""
        return await self.redis.ttl(key)

    async def expire(self, key: str, seconds: int) -> bool:
        """Set expiration on a key."""
        return await self.redis.expire(key, seconds)

    # --- Sorted Set Operations (for leaderboards) ---

    async def zadd(
        self,
        key: str,
        mapping: Dict[str, float],
        nx: bool = False,
        xx: bool = False,
    ) -> int:
        """Add members to a sorted set with scores."""
        return await self.redis.zadd(key, mapping, nx=nx, xx=xx)

    async def zrange(
        self,
        key: str,
        start: int,
        stop: int,
        withscores: bool = False,
        desc: bool = False,
    ) -> List:
        """Get range of members from sorted set."""
        if desc:
            return await self.redis.zrevrange(key, start, stop, withscores=withscores)
        return await self.redis.zrange(key, start, stop, withscores=withscores)

    async def zscore(self, key: str, member: str) -> Optional[float]:
        """Get score of member in sorted set."""
        return await self.redis.zscore(key, member)

    # --- Pub/Sub (for real-time alerts) ---

    async def publish(self, channel: str, message: Any) -> int:
        """Publish a message to a channel."""
        serialized = json.dumps(message) if not isinstance(message, str) else message
        return await self.redis.publish(channel, serialized)

    def pubsub(self):
        """Get a pubsub instance for subscribing."""
        return self.redis.pubsub()

    # --- Pipeline Support ---

    @asynccontextmanager
    async def pipeline(self, *, transaction: bool = True) -> AsyncGenerator[Pipeline]:
        """Create a Redis pipeline for batching commands.

        Use transaction=True (MULTI/EXEC) for atomic writes.
        Use transaction=False for batched reads without atomicity overhead.
        """
        pipe = self.redis.pipeline(transaction=transaction)
        try:
            yield pipe
            await pipe.execute()
        except Exception:
            await pipe.reset()
            raise

    # --- SCAN (replacement for KEYS *) ---

    async def scan_keys(self, match: str, count: int = 100) -> list[str]:
        """Scan for keys matching a pattern. Use instead of KEYS."""
        keys = []
        cursor = 0
        while True:
            cursor, batch = await self.redis.scan(cursor=cursor, match=match, count=count)
            keys.extend(batch)
            if cursor == 0:
                break
        return keys

    # --- Additional Sorted Set Operations ---

    async def zrevrange_withscores(
        self, key: str, start: int, stop: int
    ) -> list[tuple[str, float]]:
        """Get range of members from sorted set in descending order with scores."""
        return await self.redis.zrevrange(key, start, stop, withscores=True)

    async def zrevrank(self, key: str, member: str) -> int | None:
        """Get reverse rank (0-based, highest score = 0) of member in sorted set."""
        return await self.redis.zrevrank(key, member)

    async def zcount(self, key: str, min_score: float, max_score: float) -> int:
        """Count members in sorted set with scores between min and max."""
        return await self.redis.zcount(key, min_score, max_score)

    async def zcard(self, key: str) -> int:
        """Get the number of members in a sorted set."""
        return await self.redis.zcard(key)

    async def zremrangebyrank(self, key: str, start: int, stop: int) -> int:
        """Remove members by rank range."""
        return await self.redis.zremrangebyrank(key, start, stop)

    async def zremrangebyscore(
        self, key: str, min_score: float, max_score: float
    ) -> int:
        """Remove members with scores between min and max."""
        return await self.redis.zremrangebyscore(key, min_score, max_score)

    async def zrangebyscore(
        self, key: str, min_score: float, max_score: float,
        start: int = 0, num: int = -1,
    ) -> list[str]:
        """Get members with scores between min and max."""
        if num == -1:
            return await self.redis.zrangebyscore(key, min_score, max_score)
        return await self.redis.zrangebyscore(
            key, min_score, max_score, start=start, num=num,
        )

    # --- Stream Operations ---

    async def xadd(
        self, key: str, fields: dict[str, str], maxlen: int | None = None,
    ) -> str:
        """Add entry to a stream. Returns the entry ID."""
        kwargs: dict[str, Any] = {}
        if maxlen is not None:
            kwargs["maxlen"] = maxlen
            kwargs["approximate"] = True
        return await self.redis.xadd(key, fields, **kwargs)

    async def xread(
        self, streams: dict[str, str], count: int = 100, block: int | None = None,
    ) -> list:
        """Read from one or more streams."""
        return await self.redis.xread(streams, count=count, block=block)

    async def xreadgroup(
        self, group: str, consumer: str, streams: dict[str, str],
        count: int = 100, block: int | None = None,
    ) -> list:
        """Read from stream as part of a consumer group."""
        return await self.redis.xreadgroup(
            group, consumer, streams, count=count, block=block,
        )

    async def xack(self, key: str, group: str, *ids: str) -> int:
        """Acknowledge stream messages in a consumer group."""
        return await self.redis.xack(key, group, *ids)

    async def xgroup_create(
        self, key: str, group: str, id: str = "0", mkstream: bool = True,
    ) -> bool:
        """Create a consumer group for a stream."""
        try:
            return await self.redis.xgroup_create(key, group, id=id, mkstream=mkstream)
        except aioredis.ResponseError as e:
            if "BUSYGROUP" in str(e):
                return False
            raise
