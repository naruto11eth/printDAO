"""Persistent per-wallet fill history for Hyperliquid.

Two write paths feed one storage schema:
  - Backfill: paginate userFillsByTime backward for the last N days.
  - Tail: consume stream:user_events:hl events with event_type="fills".

Read path: get_fills(address, limit) materializes Fill objects from Redis,
bypassing the HL API entirely once the wallet has been backfilled. This is
the whole point of Phase B5 — existing analytics services were fetching
fills on every request and paying userFills weight + latency.

Storage:
  hl:fills:{addr}               ZSET   score=time_ms, member=tid
  hl:fill:{addr}:{tid}          HASH   raw fill fields
  hl:fills:backfill:{addr}      STR    unix ms of last completed backfill
"""

import asyncio
import json
import logging
import time
from typing import Any

from indexers.hyperliquid.client import HyperliquidClient
from indexers.hyperliquid.models import Fill
from bot.storage.redis_client import RedisClient

logger = logging.getLogger(__name__)

DEFAULT_BACKFILL_WINDOW_DAYS = 30
BACKFILL_PAGE_MAX = 2000
BACKFILL_TOTAL_MAX = 10_000
FILL_HASH_TTL = 90 * 86400
BACKFILL_SCAN_INTERVAL = 60.0

_WATCHLIST_KEY_PATTERN = "wallet:*:subscribers"


class FillHistoryService:
    """Owns both write paths (backfill + tail) and the read path.

    The read path is process-agnostic — pass an hl_client=None instance
    in a process that only needs to query fills (e.g. pure API handlers).
    """

    def __init__(
        self,
        hl_client: HyperliquidClient | None,
        redis_client: RedisClient,
        *,
        backfill_window_days: int = DEFAULT_BACKFILL_WINDOW_DAYS,
    ):
        self.hl_client = hl_client
        self.redis = redis_client
        self.backfill_window_days = backfill_window_days
        # Global serialization: backfills run one-at-a-time to respect the
        # weight budget and avoid hammering HL on a watchlist burst.
        self._backfill_lock = asyncio.Lock()
        self._in_flight: set[str] = set()
        self._scanner_task: asyncio.Task | None = None
        self._scanner_running = False
        self._backfills_ran = 0
        self._fills_persisted = 0

    # ---------- writes ----------

    async def ensure_backfilled(self, address: str, *, force: bool = False) -> bool:
        """Run one backfill for `address` unless its sentinel already exists.

        Returns True if a backfill actually ran, False if skipped.
        """
        if self.hl_client is None:
            raise RuntimeError("ensure_backfilled requires hl_client")
        address = address.lower()
        sentinel_key = f"hl:fills:backfill:{address}"
        if not force:
            existing = await self.redis.get(sentinel_key)
            if existing:
                return False

        async with self._backfill_lock:
            if address in self._in_flight:
                return False
            self._in_flight.add(address)
            try:
                count = await self._do_backfill(address)
                await self.redis.set(
                    sentinel_key, str(int(time.time() * 1000)),
                )
                self._backfills_ran += 1
                logger.info("Backfilled %d fills for %s", count, address)
                return True
            finally:
                self._in_flight.discard(address)

    async def _do_backfill(self, address: str) -> int:
        """Paginate userFillsByTime backward from now."""
        assert self.hl_client is not None  # gate is on the caller
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - (self.backfill_window_days * 86400 * 1000)

        total = 0
        end_time_ms: int | None = now_ms
        # Max pages bounded by the total cap and page size so a single wallet
        # can't starve the budget if HL keeps returning full pages.
        for _ in range(BACKFILL_TOTAL_MAX // BACKFILL_PAGE_MAX + 1):
            fills_raw = await self.hl_client.get_user_fills_by_time_raw(
                address=address,
                start_time_ms=start_ms,
                end_time_ms=end_time_ms,
            )
            if not fills_raw:
                break

            await self._store_fills(address, fills_raw)
            total += len(fills_raw)

            # Next page: oldest fill's time - 1 becomes the new end.
            times = [int(f.get("time", 0) or 0) for f in fills_raw]
            times = [t for t in times if t > 0]
            if not times:
                break
            oldest = min(times)
            if oldest <= start_ms or len(fills_raw) < BACKFILL_PAGE_MAX:
                break
            if total >= BACKFILL_TOTAL_MAX:
                logger.info("Backfill cap reached for %s at %d fills", address, total)
                break
            end_time_ms = oldest - 1

        return total

    async def handle_user_event(self, stream_name: str, fields: dict) -> None:
        """Tail consumer for stream:user_events:hl fills events."""
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
        if not fills:
            return
        await self._store_fills(address, fills)

    async def _store_fills(self, address: str, fills_raw: list[dict]) -> None:
        if not fills_raw:
            return
        r = self.redis.redis
        pipe = r.pipeline(transaction=True)
        stored = 0
        for f in fills_raw:
            tid = _extract_tid(f)
            if not tid:
                continue
            try:
                time_ms = int(f.get("time", 0) or 0)
            except (TypeError, ValueError):
                time_ms = 0
            pipe.zadd(f"hl:fills:{address}", {tid: time_ms})
            pipe.hset(
                f"hl:fill:{address}:{tid}",
                mapping={k: _coerce_str(v) for k, v in f.items() if v is not None},
            )
            pipe.expire(f"hl:fill:{address}:{tid}", FILL_HASH_TTL)
            stored += 1
        await pipe.execute()
        self._fills_persisted += stored

    # ---------- reads ----------

    async def get_fills(
        self, address: str, *, limit: int = 2000, since_ms: int = 0,
    ) -> list[Fill]:
        """Read fills from Redis. Returns chronological-desc Fill objects."""
        address = address.lower()
        r = self.redis.redis
        if since_ms > 0:
            ids = await r.zrevrangebyscore(
                f"hl:fills:{address}", "+inf", since_ms, start=0, num=limit,
            )
        else:
            ids = await r.zrevrange(f"hl:fills:{address}", 0, limit - 1)
        if not ids:
            return []

        pipe = r.pipeline(transaction=False)
        for tid in ids:
            pipe.hgetall(f"hl:fill:{address}:{tid}")
        hashes = await pipe.execute()

        fills: list[Fill] = []
        for h in hashes:
            if not h:
                continue
            try:
                fills.append(Fill.from_raw(h))
            except (KeyError, ValueError, TypeError) as e:
                logger.debug("Skipping corrupt fill hash: %s", e)
        return fills

    async def has_backfill(self, address: str) -> bool:
        sentinel_key = f"hl:fills:backfill:{address.lower()}"
        return bool(await self.redis.get(sentinel_key))

    # ---------- background scanner ----------

    async def start_scanner(self) -> None:
        """Periodically backfill any watchlist wallet without a sentinel.

        Rate-limited by the global backfill lock — no risk of flooding HL.
        """
        if self._scanner_running or self.hl_client is None:
            return
        self._scanner_running = True
        self._scanner_task = asyncio.create_task(self._scan_loop())
        logger.info("FillHistoryService scanner started")

    async def stop_scanner(self) -> None:
        self._scanner_running = False
        if self._scanner_task:
            self._scanner_task.cancel()
            try:
                await self._scanner_task
            except asyncio.CancelledError:
                pass

    async def _scan_loop(self) -> None:
        while self._scanner_running:
            try:
                await self._scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Fill backfill scan failed: %s", e)
            try:
                await asyncio.sleep(BACKFILL_SCAN_INTERVAL)
            except asyncio.CancelledError:
                break

    async def _scan_once(self) -> int:
        """One pass of the watchlist. Returns number of backfills triggered."""
        triggered = 0
        async for key in self.redis.redis.scan_iter(match=_WATCHLIST_KEY_PATTERN):
            if isinstance(key, bytes):
                key = key.decode("utf-8")
            parts = key.split(":")
            if len(parts) != 3 or parts[0] != "wallet":
                continue
            address = parts[1].lower()
            if await self.has_backfill(address):
                continue
            try:
                if await self.ensure_backfilled(address):
                    triggered += 1
            except Exception as e:
                logger.warning("Backfill failed for %s: %s", address, e)
        return triggered

    # ---------- stats ----------

    @property
    def backfills_ran(self) -> int:
        return self._backfills_ran

    @property
    def fills_persisted(self) -> int:
        return self._fills_persisted


def _extract_tid(f: dict) -> str:
    """HL fill dedup key. Prefer tid, fall back to oid or hash."""
    for k in ("tid", "oid", "hash"):
        v = f.get(k)
        if v is None or v == "":
            continue
        return str(v)
    return ""


def _coerce_str(v: Any) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v, separators=(",", ":"))
    return str(v)
