"""Consume HL user events, materialize liquidations.

Listens on stream:user_events:hl (written by Phase B2). For each event whose
event_type is "liquidation", parses the payload, stores a normalized record,
and re-emits a compact liquidation event to stream:liquidations:hl for
downstream alert consumers.

Storage layout (keys defined in shared/constants.py):
  hl:liq:{id}           HASH   one liquidation — address, liquidator, ntl_pos, ...
  hl:liquidations       ZSET   score=ts, member=id — global history, capped
  hl:liq:addr:{addr}    ZSET   score=ts, member=id — per-wallet history
"""

import json
import logging
import time
from typing import Any

from bot.services.stream_service import EventBusProducer
from bot.storage.redis_client import RedisClient

logger = logging.getLogger(__name__)

STREAM_IN = "stream:user_events:hl"
STREAM_OUT = "stream:liquidations:hl"
MAX_GLOBAL_HISTORY = 10_000
LIQ_HASH_TTL = 30 * 86400  # 30 days

# Default: flag any liquidation over $100k as notable for downstream alerting.
# Callers (alert rules engine, future consumers) can re-filter on the stream.
ALERT_MIN_NTL = 100_000.0


def _to_float(raw: Any, default: float = 0.0) -> float:
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


class LiquidationIndexer:
    """Stateless consumer — one handler call per user-events message."""

    def __init__(self, redis_client: RedisClient, producer: EventBusProducer):
        self.redis = redis_client
        self.producer = producer
        self._count = 0
        self._last_ts = 0.0

    async def handle(self, stream_name: str, fields: dict[str, str]) -> None:
        """Dispatch from EventBusConsumer. Non-liquidation events are no-ops."""
        if fields.get("event_type") != "liquidation":
            return

        address = (fields.get("address") or "").lower()
        if not address.startswith("0x"):
            return

        try:
            payload = json.loads(fields.get("payload") or "{}")
        except json.JSONDecodeError:
            logger.debug("Liquidation payload not valid JSON")
            return

        liq = payload.get("liquidation") or {}
        ntl_pos = _to_float(liq.get("liquidated_ntl_pos") or liq.get("liquidatedNtlPos"))
        account_value = _to_float(
            liq.get("liquidated_account_value") or liq.get("liquidatedAccountValue"),
        )
        liquidator = str(liq.get("liquidator") or "").lower()
        published_at = _to_float(fields.get("_published_at"), default=time.time())
        # Stream messages share a ms-resolution id — use it for scoring so a
        # single poll burst doesn't collapse multiple events to one key.
        liq_id = f"{address}:{int(published_at * 1000)}"

        hash_fields = {
            "id": liq_id,
            "address": address,
            "liquidator": liquidator,
            "ntl_pos": f"{ntl_pos:.2f}",
            "account_value": f"{account_value:.2f}",
            "ts": f"{published_at:.3f}",
        }

        r = self.redis.redis
        pipe = r.pipeline(transaction=True)
        pipe.hset(f"hl:liq:{liq_id}", mapping=hash_fields)
        pipe.expire(f"hl:liq:{liq_id}", LIQ_HASH_TTL)
        pipe.zadd("hl:liquidations", {liq_id: published_at})
        pipe.zadd(f"hl:liq:addr:{address}", {liq_id: published_at})
        # Keep the global set bounded — oldest entries age out.
        pipe.zremrangebyrank("hl:liquidations", 0, -(MAX_GLOBAL_HISTORY + 1))
        await pipe.execute()

        # Always re-emit; consumers decide their own size thresholds.
        await self.producer.publish(
            STREAM_OUT,
            {
                "id": liq_id,
                "address": address,
                "liquidator": liquidator,
                "ntl_pos": ntl_pos,
                "account_value": account_value,
                "ts": published_at,
                "notable": "1" if ntl_pos >= ALERT_MIN_NTL else "0",
            },
        )

        self._count += 1
        self._last_ts = published_at

        if ntl_pos >= ALERT_MIN_NTL:
            logger.info(
                "Liquidation indexed: %s lost $%.0f (acct $%.0f)",
                address, ntl_pos, account_value,
            )

    @property
    def indexed_count(self) -> int:
        return self._count

    @property
    def last_event_ts(self) -> float:
        return self._last_ts
