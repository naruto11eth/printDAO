"""Poll metaAndAssetCtxs on an interval; persist funding + OI time-series.

metaAndAssetCtxs is weight=2, so polling every 60s costs ~2 weight/min —
negligible against the 1200/min HL budget. One call covers every asset.

Storage: two ZSETs per coin, scored by timestamp.
  hl:funding:{coin}   member=`{ts_ms}:{rate}`   score=ts
  hl:oi:{coin}        member=`{ts_ms}:{oi}`     score=ts
TTL: 7 days, pruned on each write via ZREMRANGEBYSCORE.

Anomaly events are published to stream:funding:hl with event_type in:
  rate_flip         — funding rate changed sign since the last sample
  extreme_funding   — |rate| > EXTREME_FUNDING_ABS
  oi_spike         — OI > μ + σ·OI_SPIKE_SIGMA over rolling 24h
"""

import asyncio
import logging
import math
import time
from collections import deque
from typing import Protocol

from shared.constants import StreamName
from shared.resilience import CircuitOpenError

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 60.0
TTL_SECONDS = 7 * 86400
OI_STATS_WINDOW_SECONDS = 24 * 3600
OI_SPIKE_SIGMA = 3.0
OI_SPIKE_MIN_SAMPLES = 30   # need enough history before sigma is meaningful
# HL funding accrues hourly and is shown as a decimal rate per hour; 0.0001 =
# 0.01 %/hr ≈ 0.24 %/day. Flag anything an order of magnitude above that.
EXTREME_FUNDING_ABS = 0.001


class _ClientLike(Protocol):
    async def get_asset_contexts(self) -> dict: ...


class _RedisLike(Protocol):
    redis: object
    async def zadd(self, key: str, mapping: dict) -> int: ...


class _ProducerLike(Protocol):
    async def publish(self, stream_name: str, fields: dict) -> str: ...


class _RollingOI:
    """Rolling 24h OI samples for one coin. Deque pruned on insert."""
    __slots__ = ("_points",)

    def __init__(self) -> None:
        self._points: deque[tuple[float, float]] = deque()

    def push(self, ts: float, value: float) -> None:
        self._points.append((ts, value))
        cutoff = ts - OI_STATS_WINDOW_SECONDS
        while self._points and self._points[0][0] < cutoff:
            self._points.popleft()

    def __len__(self) -> int:
        return len(self._points)

    def stats(self) -> tuple[float, float]:
        """Return (mean, stddev) of OI values in the window."""
        if not self._points:
            return 0.0, 0.0
        values = [v for _, v in self._points]
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / len(values)
        return mean, math.sqrt(var)


class MarketStatsIndexer:
    """Poll-driven snapshotter for HL funding + open-interest."""

    def __init__(
        self,
        client: _ClientLike,
        redis_client: _RedisLike,
        producer: _ProducerLike,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        extreme_funding_abs: float = EXTREME_FUNDING_ABS,
        oi_spike_sigma: float = OI_SPIKE_SIGMA,
    ):
        self.client = client
        self.redis = redis_client
        self.producer = producer
        self.poll_interval = poll_interval
        self.extreme_funding_abs = extreme_funding_abs
        self.oi_spike_sigma = oi_spike_sigma

        self._last_funding: dict[str, float] = {}
        self._oi_rolling: dict[str, _RollingOI] = {}
        self._running = False
        self._task: asyncio.Task | None = None
        self._poll_count = 0
        self._events_emitted = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "MarketStatsIndexer started — poll=%.0fs, extreme_funding=%s",
            self.poll_interval, self.extreme_funding_abs,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self.poll_once()
            except CircuitOpenError as e:
                logger.warning("MarketStatsIndexer: %s", e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("MarketStatsIndexer poll failed: %s", e)
            try:
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break

    async def poll_once(self) -> int:
        """Single poll cycle. Returns number of coins processed."""
        ctxs = await self.client.get_asset_contexts()
        if not ctxs:
            return 0
        now = time.time()
        processed = 0
        for coin, ctx in ctxs.items():
            if await self._process_coin(coin, ctx, now):
                processed += 1
        self._poll_count += 1
        return processed

    async def _process_coin(self, coin: str, ctx, now: float) -> bool:
        funding = _decimal_to_float(getattr(ctx, "funding_rate", None))
        oi = _decimal_to_float(getattr(ctx, "open_interest", None))
        if funding is None and oi is None:
            return False

        events: list[dict] = []
        if funding is not None:
            events.extend(self._detect_funding_events(coin, funding, now))
        if oi is not None:
            events.extend(self._detect_oi_events(coin, oi, now))

        await self._persist(coin, funding, oi, now)

        for ev in events:
            await self.producer.publish(StreamName.FUNDING_HL, ev)
            self._events_emitted += 1

        return True

    def _detect_funding_events(self, coin: str, funding: float, now: float) -> list[dict]:
        events: list[dict] = []
        prev = self._last_funding.get(coin)
        if prev is not None and _sign(prev) != _sign(funding) and _sign(funding) != 0:
            events.append({
                "coin": coin,
                "event_type": "rate_flip",
                "prev_funding": prev,
                "funding": funding,
                "ts": now,
            })
        self._last_funding[coin] = funding

        if abs(funding) >= self.extreme_funding_abs:
            events.append({
                "coin": coin,
                "event_type": "extreme_funding",
                "funding": funding,
                "threshold": self.extreme_funding_abs,
                "ts": now,
            })
        return events

    def _detect_oi_events(self, coin: str, oi: float, now: float) -> list[dict]:
        events: list[dict] = []
        rolling = self._oi_rolling.setdefault(coin, _RollingOI())
        if len(rolling) >= OI_SPIKE_MIN_SAMPLES:
            mean, stddev = rolling.stats()
            if stddev > 0 and oi > mean + self.oi_spike_sigma * stddev:
                events.append({
                    "coin": coin,
                    "event_type": "oi_spike",
                    "oi": oi,
                    "mean_oi": mean,
                    "stddev_oi": stddev,
                    "sigma": (oi - mean) / stddev,
                    "ts": now,
                })
        rolling.push(now, oi)
        return events

    async def _persist(self, coin: str, funding: float | None, oi: float | None,
                       now: float) -> None:
        ts_ms = int(now * 1000)
        prune_cutoff = now - TTL_SECONDS

        r = self.redis.redis
        pipe = r.pipeline(transaction=True)
        if funding is not None:
            funding_key = f"hl:funding:{coin}"
            pipe.zadd(funding_key, {f"{ts_ms}:{funding}": now})
            pipe.expire(funding_key, TTL_SECONDS)
            pipe.zremrangebyscore(funding_key, 0, prune_cutoff)
        if oi is not None:
            oi_key = f"hl:oi:{coin}"
            pipe.zadd(oi_key, {f"{ts_ms}:{oi}": now})
            pipe.expire(oi_key, TTL_SECONDS)
            pipe.zremrangebyscore(oi_key, 0, prune_cutoff)
        await pipe.execute()

    @property
    def poll_count(self) -> int:
        return self._poll_count

    @property
    def events_emitted(self) -> int:
        return self._events_emitted


def _decimal_to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sign(x: float) -> int:
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0
