"""Redis Streams service for durable trade event delivery.

Replaces fire-and-forget PUBLISH with durable streams that survive bot restarts.
Consumer groups ensure at-least-once delivery.
"""

import asyncio
import json
import logging
import time

from bot.storage.redis_client import RedisClient

logger = logging.getLogger(__name__)

STREAM_KEY = "stream:trades:{exchange}"
GROUP_NAME = "alert_processors"
CONSUMER_NAME = "bot-worker-1"
MAX_STREAM_LEN = 50_000
PENDING_TIMEOUT_MS = 3_600_000  # 1 hour


class TradeStreamProducer:
    """Writes trade events to Redis Streams."""

    def __init__(self, redis: RedisClient):
        self.redis = redis

    async def publish_trade(self, exchange: str, trade_data: dict) -> str:
        """Publish a trade event to the exchange stream.

        Returns the stream entry ID.
        """
        key = STREAM_KEY.format(exchange=exchange)
        fields = {k: str(v) if not isinstance(v, str) else v for k, v in trade_data.items()}
        fields["_published_at"] = str(time.time())
        return await self.redis.xadd(key, fields, maxlen=MAX_STREAM_LEN)

    async def publish_whale_alert(self, exchange: str, alert_data: dict) -> str:
        """Publish a whale alert event."""
        alert_data["_event_type"] = "whale_alert"
        return await self.publish_trade(exchange, alert_data)


class TradeStreamConsumer:
    """Reads trade events from Redis Streams using consumer groups.

    Ensures at-least-once delivery — messages are acknowledged only after
    successful processing. Unacknowledged messages are redelivered on restart.
    """

    def __init__(self, redis: RedisClient, handler=None):
        self.redis = redis
        self.handler = handler
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self, exchanges: list[str] | None = None) -> None:
        """Start consuming from trade streams."""
        exchanges = exchanges or ["hyperliquid", "polymarket"]
        self._running = True

        for exchange in exchanges:
            key = STREAM_KEY.format(exchange=exchange)
            await self.redis.xgroup_create(key, GROUP_NAME, id="0", mkstream=True)

        self._task = asyncio.create_task(self._consume_loop(exchanges))
        logger.info(f"Trade stream consumer started for {exchanges}")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _consume_loop(self, exchanges: list[str]) -> None:
        """Main consumer loop — reads new messages and processes them."""
        streams = {STREAM_KEY.format(exchange=ex): ">" for ex in exchanges}

        while self._running:
            try:
                # Read new messages (block up to 5s)
                entries = await self.redis.xreadgroup(
                    GROUP_NAME, CONSUMER_NAME, streams, count=50, block=5000,
                )

                if not entries:
                    continue

                for stream_name, messages in entries:
                    exchange = stream_name.split(":")[2] if ":" in stream_name else "unknown"
                    for msg_id, fields in messages:
                        try:
                            if self.handler:
                                await self.handler(exchange, fields)
                            await self.redis.xack(stream_name, GROUP_NAME, msg_id)
                        except Exception as e:
                            logger.error(f"Failed to process stream message {msg_id}: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Stream consumer error: {e}")
                await asyncio.sleep(1)

        # Reclaim pending messages on shutdown attempt
        await self._reclaim_pending(exchanges)

    async def _reclaim_pending(self, exchanges: list[str]) -> None:
        """Reclaim messages that were pending too long (dead consumer recovery)."""
        for exchange in exchanges:
            key = STREAM_KEY.format(exchange=exchange)
            try:
                pending = await self.redis.redis.xpending_range(
                    key, GROUP_NAME, min="-", max="+", count=100,
                )
                stale_ids = [
                    p["message_id"] for p in pending
                    if p.get("time_since_delivered", 0) > PENDING_TIMEOUT_MS
                ]
                if stale_ids:
                    claimed = await self.redis.redis.xclaim(
                        key, GROUP_NAME, CONSUMER_NAME,
                        min_idle_time=PENDING_TIMEOUT_MS,
                        message_ids=stale_ids,
                    )
                    for msg_id, fields in claimed:
                        try:
                            if self.handler:
                                await self.handler(exchange, fields)
                            await self.redis.xack(key, GROUP_NAME, msg_id)
                        except Exception as e:
                            logger.error(f"Failed to process reclaimed message {msg_id}: {e}")

                    logger.info(f"Reclaimed {len(claimed)} pending messages for {exchange}")
            except Exception as e:
                logger.debug(f"Pending reclaim failed for {exchange}: {e}")
