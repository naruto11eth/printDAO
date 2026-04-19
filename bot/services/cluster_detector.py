"""TWAP/stealth-accumulation detector over stream:user_events:hl fills.

Pattern: a wallet splits a large order across many small fills on the same
side within a short window — a classic TWAP or iceberg shape. On its own,
each small fill doesn't trip the whale threshold. Grouped, it's a clear
accumulation or distribution signal.

Rule (configurable, sensible defaults):
  - window: 300 s
  - min fills in window: 5
  - min total notional: $100,000
  - all fills same side

State is in-memory — consumer-group replay would re-run against pre-
persisted fills on restart, which is actually what we want: detection
catches up without needing Redis state for the deque. After an emission
the state for that (address, coin) is reset so one cluster doesn't
re-fire on every subsequent fill.

Emits to stream:clusters:hl with the compact envelope; full details are
persisted to hl:clusters:{addr} + hl:cluster:{id} so /clusters can walk
back through history.
"""

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any, Protocol

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_SECONDS = 300
DEFAULT_MIN_FILLS = 5
DEFAULT_MIN_NOTIONAL = 100_000.0
CLUSTER_TTL = 30 * 86400
MAX_GLOBAL_CLUSTERS = 5_000

STREAM_IN = "stream:user_events:hl"
STREAM_OUT = "stream:clusters:hl"


class _ProducerLike(Protocol):
    async def publish(self, stream_name: str, fields: dict[str, Any]) -> str: ...


class _RedisLike(Protocol):
    redis: object


class ClusterDetector:
    """Stateless against Redis. In-memory deque per (address, coin)."""

    def __init__(
        self,
        redis_client: _RedisLike,
        producer: _ProducerLike,
        *,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        min_fills: int = DEFAULT_MIN_FILLS,
        min_notional: float = DEFAULT_MIN_NOTIONAL,
    ):
        self.redis = redis_client
        self.producer = producer
        self.window_seconds = window_seconds
        self.min_fills = min_fills
        self.min_notional = min_notional
        self._state: dict[tuple[str, str], deque[tuple[float, float, str]]] = {}
        self._clusters_emitted = 0

    async def handle(self, stream_name: str, fields: dict[str, str]) -> None:
        """Consumer entry — called by EventBusConsumer for each message."""
        if fields.get("event_type") != "fills":
            return
        address = (fields.get("address") or "").lower()
        if not address.startswith("0x"):
            return
        try:
            payload = json.loads(fields.get("payload") or "{}")
        except json.JSONDecodeError:
            return
        fills = payload.get("fills") or []
        if not isinstance(fills, list) or not fills:
            return

        for f in fills:
            cluster = self._push_fill(address, f)
            if cluster is not None:
                await self._emit_cluster(*cluster)

    # ---------- core logic ----------

    def _push_fill(
        self, address: str, f: dict,
    ) -> tuple[str, str, list[tuple[float, float, str]], str] | None:
        """Return (address, coin, entries, side) if this fill closed a cluster."""
        coin = str(f.get("coin") or "")
        side = str(f.get("side") or "")
        if not coin or side not in ("B", "A"):
            return None

        try:
            size = float(f.get("sz") or 0)
            price = float(f.get("px") or 0)
            time_ms = int(f.get("time") or 0)
        except (TypeError, ValueError):
            return None

        notional = size * price
        if notional <= 0 or time_ms <= 0:
            return None
        ts = time_ms / 1000.0

        key = (address, coin)
        q = self._state.setdefault(key, deque())
        q.append((ts, notional, side))

        cutoff = ts - self.window_seconds
        while q and q[0][0] < cutoff:
            q.popleft()

        if len(q) < self.min_fills:
            return None

        sides = {entry[2] for entry in q}
        if len(sides) != 1:
            return None

        total_notional = sum(entry[1] for entry in q)
        if total_notional < self.min_notional:
            return None

        # Reset the deque for this key so we don't re-emit on every subsequent
        # fill that extends the cluster by one.
        entries = list(q)
        self._state[key] = deque()
        return address, coin, entries, sides.pop()

    async def _emit_cluster(
        self,
        address: str,
        coin: str,
        entries: list[tuple[float, float, str]],
        side: str,
    ) -> None:
        total_notional = sum(n for _, n, _ in entries)
        first_ts = entries[0][0]
        last_ts = entries[-1][0]
        now = time.time()
        cluster_id = f"{address}:{coin}:{int(last_ts * 1000)}"
        direction = "accumulation" if side == "B" else "distribution"

        event: dict[str, Any] = {
            "id": cluster_id,
            "address": address,
            "coin": coin,
            "side": side,
            "direction": direction,
            "fill_count": len(entries),
            "total_notional": total_notional,
            "avg_notional": total_notional / len(entries),
            "duration_seconds": last_ts - first_ts,
            "first_ts": first_ts,
            "last_ts": last_ts,
            "ts": now,
        }

        # Persist: per-address ZSET + per-cluster hash + global ZSET (bounded).
        r = self.redis.redis
        pipe = r.pipeline(transaction=True)
        pipe.hset(
            f"hl:cluster:{cluster_id}",
            mapping={k: _to_str(v) for k, v in event.items()},
        )
        pipe.expire(f"hl:cluster:{cluster_id}", CLUSTER_TTL)
        pipe.zadd(f"hl:clusters:{address}", {cluster_id: last_ts})
        pipe.zadd("hl:clusters", {cluster_id: last_ts})
        pipe.zremrangebyrank("hl:clusters", 0, -(MAX_GLOBAL_CLUSTERS + 1))
        await pipe.execute()

        await self.producer.publish(STREAM_OUT, event)
        self._clusters_emitted += 1

        logger.info(
            "Cluster: %s %s %s — %d fills, $%.0f over %.0fs",
            address, coin, direction, len(entries),
            total_notional, last_ts - first_ts,
        )

    # ---------- stats ----------

    @property
    def clusters_emitted(self) -> int:
        return self._clusters_emitted

    @property
    def tracked_keys(self) -> int:
        return len(self._state)


def _to_str(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.6f}"
    return str(v)
