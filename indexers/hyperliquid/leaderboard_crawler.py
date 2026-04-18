"""Polls the HL official leaderboard stats endpoint.

Source: GET https://stats-data.hyperliquid.xyz/Mainnet/leaderboard — this is
what app.hyperliquid.xyz/leaderboard calls. No auth, no SDK, ~28MB payload
with ~34k trader rows.

We only persist the top N rows (default 500) ranked by the configured window
(day / week / month / allTime). Two keys are kept in sync:

  hl:top_traders          ZSET  score=pnl           member=address
  hl:top_trader:{addr}    HASH  full per-trader metrics (all 4 windows)

On each successful poll we publish to stream:top_traders_updated:hl so the
downstream userEvents subscriber (Phase B2) can refresh its WS subscription
set without needing to poll Redis.

The ZSET is rebuilt atomically via a scratch key + RENAME — readers never
see a partial state. Address hashes are written with a TTL and refreshed on
each poll; if an address drops out of the top N for longer than TTL, its
hash is naturally GC'd.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Iterable

import httpx

from bot.services.stream_service import EventBusProducer
from bot.storage.redis_client import RedisClient
from shared.constants import StreamName
from shared.resilience import CircuitBreaker, CircuitOpenError

logger = logging.getLogger(__name__)

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
VALID_WINDOWS = ("day", "week", "month", "allTime")
SCRATCH_KEY = "hl:top_traders:_scratch"
TOP_TRADER_TTL = 7 * 86400  # 7 days — outlives the 5-min poll window


@dataclass(frozen=True)
class _LeaderRow:
    address: str
    account_value: float
    display_name: str
    prize: int
    # {window: {pnl, roi, vlm}}
    windows: dict[str, dict[str, float]]

    def window_pnl(self, window: str) -> float:
        return self.windows.get(window, {}).get("pnl", 0.0)

    def to_hash(self) -> dict[str, str]:
        base: dict[str, str] = {
            "address": self.address,
            "account_value": f"{self.account_value:.2f}",
            "display_name": self.display_name,
            "prize": str(self.prize),
        }
        for window, metrics in self.windows.items():
            base[f"{window}_pnl"] = f"{metrics.get('pnl', 0.0):.2f}"
            base[f"{window}_roi"] = f"{metrics.get('roi', 0.0):.6f}"
            base[f"{window}_vlm"] = f"{metrics.get('vlm', 0.0):.2f}"
        return base


def _parse_rows(raw_rows: Iterable[dict]) -> list[_LeaderRow]:
    out: list[_LeaderRow] = []
    for row in raw_rows:
        try:
            address = str(row["ethAddress"]).lower()
            if not address.startswith("0x"):
                continue
            raw_windows = row.get("windowPerformances")
            if not isinstance(raw_windows, list):
                continue
            windows: dict[str, dict[str, float]] = {}
            for entry in raw_windows:
                if not (isinstance(entry, list) and len(entry) == 2):
                    continue
                window, metrics = entry
                if window not in VALID_WINDOWS or not isinstance(metrics, dict):
                    continue
                windows[window] = {
                    "pnl": float(metrics.get("pnl", 0) or 0),
                    "roi": float(metrics.get("roi", 0) or 0),
                    "vlm": float(metrics.get("vlm", 0) or 0),
                }
            if not windows:
                continue
            out.append(_LeaderRow(
                address=address,
                account_value=float(row.get("accountValue", 0) or 0),
                display_name=str(row.get("displayName") or ""),
                prize=int(row.get("prize") or 0),
                windows=windows,
            ))
        except (KeyError, ValueError, TypeError) as e:
            logger.debug("Skipping malformed leaderboard row: %s", e)
    return out


class LeaderboardCrawler:
    """Polls the HL leaderboard endpoint on an interval.

    Rank window (day/week/month/allTime) is chosen at construction. Reasonable
    defaults: allTime for stable whale coverage, week for momentum plays.
    """

    def __init__(
        self,
        redis_client: RedisClient,
        producer: EventBusProducer,
        *,
        top_n: int = 500,
        poll_interval: float = 300.0,
        rank_by: str = "allTime",
        request_timeout: float = 30.0,
    ):
        if rank_by not in VALID_WINDOWS:
            raise ValueError(
                f"rank_by must be one of {VALID_WINDOWS}, got {rank_by!r}",
            )
        self.redis = redis_client
        self.producer = producer
        self.top_n = top_n
        self.poll_interval = poll_interval
        self.rank_by = rank_by
        self.request_timeout = request_timeout

        self._running = False
        self._task: asyncio.Task | None = None
        self._breaker = CircuitBreaker(
            name="hl_leaderboard",
            failure_threshold=3,
            recovery_timeout=120.0,
        )
        self._last_success_ts: float = 0.0
        self._last_row_count: int = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "LeaderboardCrawler started — top_n=%d, rank_by=%s, interval=%.0fs",
            self.top_n, self.rank_by, self.poll_interval,
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
                await self._poll_once()
            except CircuitOpenError as e:
                logger.warning("LeaderboardCrawler: %s", e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("LeaderboardCrawler poll failed: %s", e)
            try:
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break

    async def _poll_once(self) -> None:
        async def fetch() -> dict:
            async with httpx.AsyncClient(timeout=self.request_timeout) as client:
                resp = await client.get(
                    LEADERBOARD_URL,
                    headers={"User-Agent": "printdao-indexer/1.0"},
                )
                resp.raise_for_status()
                return resp.json()

        payload = await self._breaker.call(fetch)
        raw_rows = payload.get("leaderboardRows") or []
        if not raw_rows:
            logger.warning("Leaderboard returned no rows")
            return

        rows = _parse_rows(raw_rows)
        rows.sort(key=lambda r: r.window_pnl(self.rank_by), reverse=True)
        top = rows[: self.top_n]

        await self._persist(top)

        self._last_success_ts = time.time()
        self._last_row_count = len(top)

        await self.producer.publish(
            StreamName.TOP_TRADERS_UPDATED,
            {
                "ts": self._last_success_ts,
                "count": len(top),
                "rank_by": self.rank_by,
            },
        )

        logger.info(
            "LeaderboardCrawler: ingested %d rows → top %d (rank_by=%s)",
            len(rows), len(top), self.rank_by,
        )

    async def _persist(self, top: list[_LeaderRow]) -> None:
        """Swap the ZSET atomically and upsert per-address hashes."""
        if not top:
            return

        r = self.redis.redis

        # Build scratch ZSET
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.delete(SCRATCH_KEY)
            zmap = {row.address: row.window_pnl(self.rank_by) for row in top}
            pipe.zadd(SCRATCH_KEY, zmap)
            pipe.rename(SCRATCH_KEY, "hl:top_traders")
            pipe.set("hl:top_traders:updated_at", int(time.time()))

        # Upsert hashes + refresh TTL. Not in a transaction — partial progress
        # is fine here since the ZSET is the source of truth for membership.
        async with self.redis.pipeline(transaction=False) as pipe:
            for row in top:
                key = f"hl:top_trader:{row.address}"
                pipe.hset(key, mapping=row.to_hash())
                pipe.expire(key, TOP_TRADER_TTL)

    @property
    def last_success_ts(self) -> float:
        return self._last_success_ts

    @property
    def last_row_count(self) -> int:
        return self._last_row_count
