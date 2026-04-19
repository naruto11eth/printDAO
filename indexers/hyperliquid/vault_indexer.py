"""Vault discovery + per-vault snapshot indexer for Hyperliquid.

Discovery runs off the same undocumented but stable stats host we use for the
trader leaderboard:
  GET https://stats-data.hyperliquid.xyz/Mainnet/vaults   — ~14MB, ~9k vaults

Per-vault deep state comes from the authoritative info endpoint:
  POST /info {"type":"vaultDetails","vaultAddress":"0x…"}  — weight 2

Summaries run every 5 min (cheap HTTP, no REST weight). Details poll only the
top N by TVL every 2 min + always HLP. We detect flow on TVL deltas against
the previous snapshot — absolute (≥$1M) and relative (≥5%) thresholds; HLP
uses the absolute threshold only since even small % moves are significant.
"""

import asyncio
import logging
import time
from typing import Protocol

import httpx

from bot.services.stream_service import EventBusProducer
from bot.storage.redis_client import RedisClient
from shared.constants import StreamName
from shared.resilience import CircuitBreaker, CircuitOpenError

logger = logging.getLogger(__name__)

VAULTS_URL = "https://stats-data.hyperliquid.xyz/Mainnet/vaults"
HLP_ADDRESS = "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"

DEFAULT_SUMMARIES_INTERVAL = 300.0
DEFAULT_DETAILS_INTERVAL = 120.0
DEFAULT_TOP_N = 50
SUMMARY_TTL = 7 * 86400
SNAPSHOTS_TTL = 30 * 86400
FLOW_ABS_THRESHOLD_USD = 1_000_000.0
FLOW_REL_THRESHOLD = 0.05
SCRATCH_KEY = "hl:vaults:_scratch"


class _ClientLike(Protocol):
    async def get_vault_details(self, vault_address: str) -> dict: ...


class VaultIndexer:
    def __init__(
        self,
        client: _ClientLike,
        redis_client: RedisClient,
        producer: EventBusProducer,
        *,
        summaries_interval: float = DEFAULT_SUMMARIES_INTERVAL,
        details_interval: float = DEFAULT_DETAILS_INTERVAL,
        top_n: int = DEFAULT_TOP_N,
        request_timeout: float = 30.0,
    ):
        self.client = client
        self.redis = redis_client
        self.producer = producer
        self.summaries_interval = summaries_interval
        self.details_interval = details_interval
        self.top_n = top_n
        self.request_timeout = request_timeout

        self._running = False
        self._summaries_task: asyncio.Task | None = None
        self._details_task: asyncio.Task | None = None
        self._breaker = CircuitBreaker(
            name="hl_vaults", failure_threshold=3, recovery_timeout=120.0,
        )
        self._last_summaries_ts = 0.0
        self._last_summaries_count = 0
        self._details_polled = 0
        self._flow_events = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._summaries_task = asyncio.create_task(self._summaries_loop())
        self._details_task = asyncio.create_task(self._details_loop())
        logger.info(
            "VaultIndexer started — top_n=%d, summaries=%.0fs, details=%.0fs",
            self.top_n, self.summaries_interval, self.details_interval,
        )

    async def stop(self) -> None:
        self._running = False
        for task in (self._summaries_task, self._details_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    # ---------- summaries loop ----------

    async def _summaries_loop(self) -> None:
        while self._running:
            try:
                await self.poll_summaries_once()
            except CircuitOpenError as e:
                logger.warning("vault summaries: %s", e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("vault summaries poll failed: %s", e)
            try:
                await asyncio.sleep(self.summaries_interval)
            except asyncio.CancelledError:
                break

    async def poll_summaries_once(self) -> int:
        async def fetch():
            async with httpx.AsyncClient(timeout=self.request_timeout) as client:
                r = await client.get(
                    VAULTS_URL, headers={"User-Agent": "printdao-indexer/1.0"},
                )
                r.raise_for_status()
                return r.json()

        payload = await self._breaker.call(fetch)
        if not isinstance(payload, list):
            return 0

        entries = list(_parse_summaries(payload))
        if not entries:
            return 0

        await self._persist_summaries(entries)
        self._last_summaries_ts = time.time()
        self._last_summaries_count = len(entries)
        logger.info("VaultIndexer: %d vault summaries ingested", len(entries))
        return len(entries)

    async def _persist_summaries(
        self, entries: list[tuple[str, float, dict, float]],
    ) -> None:
        r = self.redis.redis
        pipe = r.pipeline(transaction=True)

        # Atomic rank swap via scratch key.
        pipe.delete(SCRATCH_KEY)
        zmap = {addr: tvl for addr, tvl, _, _ in entries}
        pipe.zadd(SCRATCH_KEY, zmap)
        pipe.rename(SCRATCH_KEY, "hl:vaults")

        for addr, tvl, summary, apr in entries:
            pipe.hset(
                f"hl:vault:{addr}:summary",
                mapping={
                    "address": addr,
                    "name": str(summary.get("name") or ""),
                    "leader": str(summary.get("leader") or "").lower(),
                    "tvl": f"{tvl:.2f}",
                    "is_closed": "1" if summary.get("isClosed") else "0",
                    "create_time_ms": str(summary.get("createTimeMillis") or 0),
                    "relationship": str(
                        (summary.get("relationship") or {}).get("type") or "",
                    ),
                    "apr": f"{apr:.6f}",
                },
            )
            pipe.expire(f"hl:vault:{addr}:summary", SUMMARY_TTL)
        await pipe.execute()

    # ---------- details loop ----------

    async def _details_loop(self) -> None:
        while self._running:
            try:
                await self.poll_top_details_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("vault details poll failed: %s", e)
            try:
                await asyncio.sleep(self.details_interval)
            except asyncio.CancelledError:
                break

    async def poll_top_details_once(self) -> int:
        top = await self.redis.redis.zrevrange("hl:vaults", 0, self.top_n - 1)
        targets = [addr.lower() for addr in (top or [])]
        if HLP_ADDRESS not in targets:
            targets.insert(0, HLP_ADDRESS)

        polled = 0
        for addr in targets:
            try:
                details = await self.client.get_vault_details(addr)
            except Exception as e:
                logger.debug("vaultDetails failed for %s: %s", addr, e)
                continue
            if details:
                await self._store_details(addr, details)
                polled += 1
        return polled

    async def _store_details(self, addr: str, details: dict) -> None:
        now = time.time()
        ts_ms = int(now * 1000)

        tvl = _extract_current_tvl(details)
        followers = details.get("followers") or []
        follower_count = len(followers) if isinstance(followers, list) else 0

        snapshot_key = f"hl:vault:{addr}:snapshots"
        prev_tvl = await _read_last_snapshot_tvl(self.redis, snapshot_key)

        r = self.redis.redis
        pipe = r.pipeline(transaction=True)
        pipe.zadd(snapshot_key, {f"{ts_ms}:{tvl}:{follower_count}": now})
        pipe.expire(snapshot_key, SNAPSHOTS_TTL)
        pipe.zremrangebyscore(snapshot_key, 0, now - SNAPSHOTS_TTL)
        await pipe.execute()
        self._details_polled += 1

        if prev_tvl <= 0:
            return  # first snapshot — no delta to compute

        delta_tvl = tvl - prev_tvl
        rel = delta_tvl / prev_tvl if prev_tvl > 0 else 0.0
        notable_abs = abs(delta_tvl) >= FLOW_ABS_THRESHOLD_USD
        notable_rel = abs(rel) >= FLOW_REL_THRESHOLD
        # HLP: absolute threshold only — % moves are always small at that scale.
        if addr == HLP_ADDRESS:
            notable = notable_abs
        else:
            notable = notable_abs and notable_rel

        if notable:
            await self.producer.publish(StreamName.VAULT_FLOW_HL, {
                "vault_addr": addr,
                "event_type": "vault_flow",
                "tvl": tvl,
                "prev_tvl": prev_tvl,
                "delta_tvl": delta_tvl,
                "rel_change": rel,
                "direction": "inflow" if delta_tvl > 0 else "outflow",
                "follower_count": follower_count,
                "ts": now,
            })
            self._flow_events += 1

    # ---------- stats ----------

    @property
    def last_summaries_count(self) -> int:
        return self._last_summaries_count

    @property
    def details_polled(self) -> int:
        return self._details_polled

    @property
    def flow_events(self) -> int:
        return self._flow_events


# ---------- pure helpers (kept module-level for testability) ----------

def _parse_summaries(payload: list) -> list[tuple[str, float, dict, float]]:
    """Filter + normalize a stats-data vaults payload.

    Each output tuple: (address_lower, tvl, summary_dict, apr).
    """
    out: list[tuple[str, float, dict, float]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        summary = entry.get("summary") or {}
        addr = str(summary.get("vaultAddress") or "").lower()
        if not addr.startswith("0x") or len(addr) != 42:
            continue
        try:
            tvl = float(summary.get("tvl") or 0)
        except (TypeError, ValueError):
            tvl = 0.0
        try:
            apr = float(entry.get("apr") or 0)
        except (TypeError, ValueError):
            apr = 0.0
        out.append((addr, tvl, summary, apr))
    return out


def _extract_current_tvl(details: dict) -> float:
    """Most recent accountValue from portfolio.day.accountValueHistory."""
    portfolio = details.get("portfolio") or []
    if not isinstance(portfolio, list):
        return 0.0
    for entry in portfolio:
        if not (isinstance(entry, list) and len(entry) == 2):
            continue
        window, data = entry
        if window != "day":
            continue
        history = (data or {}).get("accountValueHistory") or []
        if not history:
            return 0.0
        last = history[-1]
        try:
            return float(last[1])
        except (IndexError, TypeError, ValueError):
            return 0.0
    return 0.0


async def _read_last_snapshot_tvl(
    redis_client: RedisClient, snapshot_key: str,
) -> float:
    """Parse the newest '{ts_ms}:{tvl}:{follower_count}' member's TVL."""
    prev_raw = await redis_client.redis.zrevrange(snapshot_key, 0, 0)
    if not prev_raw:
        return 0.0
    parts = str(prev_raw[0]).split(":")
    if len(parts) < 2:
        return 0.0
    try:
        return float(parts[1])
    except ValueError:
        return 0.0
