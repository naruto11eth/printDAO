"""Redis Streams event bus for durable event delivery across indexers.

Replaces fire-and-forget PUBLISH with durable streams that survive bot restarts.
Consumer groups ensure at-least-once delivery.

Originally scoped to trade events only (Phase 6). Generalized in Phase A1 to
carry arbitrary event types — user_events, liquidations, funding, vault flows,
clusters, etc. Each caller supplies the target stream name; the bus doesn't
hardcode stream topology.
"""

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from bot.storage.redis_client import RedisClient

logger = logging.getLogger(__name__)

DEFAULT_GROUP = "alert_processors"
DEFAULT_CONSUMER = "bot-worker-1"
DEFAULT_MAX_STREAM_LEN = 50_000
PENDING_TIMEOUT_MS = 3_600_000  # 1 hour

# Back-compat: the original trade stream key pattern.
TRADE_STREAM_KEY = "stream:trades:{exchange}"

Handler = Callable[[str, dict[str, str]], Awaitable[None]]


def compose_handlers(*handlers: Handler) -> Handler:
    """Run multiple handlers sequentially for one stream message.

    Each handler's exceptions are logged and swallowed so the message
    is xack'd once all have been attempted. Consumer-group replay would
    re-run every handler otherwise, which is wasteful (and incorrect
    for any handler that's already-persisted its output on an earlier
    attempt).
    """
    async def composed(stream_name: str, fields: dict[str, str]) -> None:
        for h in handlers:
            try:
                await h(stream_name, fields)
            except Exception as e:
                logger.error(
                    "composed handler %s failed on %s: %s",
                    getattr(h, "__qualname__", "<handler>"), stream_name, e,
                )
    return composed


class EventBusProducer:
    """Writes events to arbitrary Redis Streams.

    Callers pick their stream name — the producer is agnostic. Helpers like
    publish_trade are preserved for existing indexer code paths.
    """

    def __init__(self, redis: RedisClient):
        self.redis = redis

    async def publish(
        self,
        stream_name: str,
        fields: dict[str, Any],
        *,
        maxlen: int = DEFAULT_MAX_STREAM_LEN,
    ) -> str:
        """Publish one event to a stream. Returns the entry ID.

        All values are coerced to str since Redis stream fields are string-only.
        Add a `_published_at` wall-clock timestamp for consumer-side lag calc.
        """
        encoded = {k: v if isinstance(v, str) else str(v) for k, v in fields.items()}
        encoded["_published_at"] = str(time.time())
        return await self.redis.xadd(stream_name, encoded, maxlen=maxlen)

    async def publish_trade(self, exchange: str, trade_data: dict[str, Any]) -> str:
        """Back-compat helper for the Phase 6 trade-event path."""
        return await self.publish(
            TRADE_STREAM_KEY.format(exchange=exchange), trade_data,
        )

    async def publish_whale_alert(self, exchange: str, alert_data: dict[str, Any]) -> str:
        alert_data = dict(alert_data)
        alert_data["_event_type"] = "whale_alert"
        return await self.publish_trade(exchange, alert_data)


class EventBusConsumer:
    """Reads events from one or more streams using consumer groups.

    Register handlers per stream: `{"stream:trades:hl": handle_trade, ...}`.
    At-least-once delivery — messages are xack'd only after the handler returns
    without raising. Stale pending entries are reclaimed on shutdown attempts.
    """

    def __init__(
        self,
        redis: RedisClient,
        *,
        handlers: dict[str, Handler] | None = None,
        group: str = DEFAULT_GROUP,
        consumer: str = DEFAULT_CONSUMER,
    ):
        self.redis = redis
        self._handlers: dict[str, Handler] = dict(handlers or {})
        self.group = group
        self.consumer = consumer
        self._running = False
        self._task: asyncio.Task | None = None

    def register(self, stream_name: str, handler: Handler) -> None:
        self._handlers[stream_name] = handler

    async def start(self) -> None:
        if self._running or not self._handlers:
            if not self._handlers:
                logger.warning("EventBusConsumer started with no handlers — skipping")
            return
        self._running = True

        for stream_name in self._handlers:
            await self.redis.xgroup_create(stream_name, self.group, id="0", mkstream=True)

        self._task = asyncio.create_task(self._consume_loop())
        logger.info("EventBusConsumer started for streams: %s", list(self._handlers))

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _consume_loop(self) -> None:
        streams = {name: ">" for name in self._handlers}

        while self._running:
            try:
                entries = await self.redis.xreadgroup(
                    self.group, self.consumer, streams, count=50, block=5000,
                )
                if not entries:
                    continue

                for stream_name, messages in entries:
                    handler = self._handlers.get(stream_name)
                    if handler is None:
                        logger.warning("No handler for stream %s", stream_name)
                        continue
                    for msg_id, fields in messages:
                        try:
                            await handler(stream_name, fields)
                            await self.redis.xack(stream_name, self.group, msg_id)
                        except Exception as e:
                            logger.error(
                                "Handler failed for %s/%s: %s", stream_name, msg_id, e,
                            )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("EventBusConsumer loop error: %s", e)
                await asyncio.sleep(1)

        await self._reclaim_pending()

    async def _reclaim_pending(self) -> None:
        """Reclaim messages pending longer than PENDING_TIMEOUT_MS."""
        for stream_name, handler in self._handlers.items():
            try:
                pending = await self.redis.redis.xpending_range(
                    stream_name, self.group, min="-", max="+", count=100,
                )
                stale_ids = [
                    p["message_id"] for p in pending
                    if p.get("time_since_delivered", 0) > PENDING_TIMEOUT_MS
                ]
                if not stale_ids:
                    continue
                claimed = await self.redis.redis.xclaim(
                    stream_name, self.group, self.consumer,
                    min_idle_time=PENDING_TIMEOUT_MS,
                    message_ids=stale_ids,
                )
                for msg_id, fields in claimed:
                    try:
                        await handler(stream_name, fields)
                        await self.redis.xack(stream_name, self.group, msg_id)
                    except Exception as e:
                        logger.error(
                            "Reclaim handler failed for %s/%s: %s",
                            stream_name, msg_id, e,
                        )
                logger.info("Reclaimed %d pending messages on %s", len(claimed), stream_name)
            except Exception as e:
                logger.debug("Pending reclaim failed for %s: %s", stream_name, e)


# Back-compat aliases for existing imports (bot/main.py et al.).
TradeStreamProducer = EventBusProducer
TradeStreamConsumer = EventBusConsumer
