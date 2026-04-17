"""Rolling-window rate-limit weight budget for HL REST calls.

HL enforces 1200 weight/minute per IP. Each endpoint has a documented weight
(see HL_ENDPOINT_WEIGHTS in shared/constants.py). Without proactive accounting,
15 coins polling recentTrades every 5s exceeds the cap by 3x — we either get
silently throttled or hit 429s.

The budget tracks a rolling 60s window of (timestamp, weight) points. Before
each HL call, reserve() confirms the window has headroom; if not, it sleeps
until the oldest entry ages out enough to fit the new weight. No locks around
the asyncio.sleep — other coroutines may proceed while we wait.
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60.0


@dataclass
class _BudgetStats:
    reservations: int = 0
    total_weight: int = 0
    total_wait_seconds: float = 0.0
    max_wait_seconds: float = 0.0
    last_log_time: float = field(default_factory=time.monotonic)


class WeightBudget:
    """Proactive rate-limit accounting for a single exchange.

    Not exchange-specific by design — HL-specific defaults live in
    shared/constants.py and are injected at call sites.
    """

    def __init__(self, name: str, cap_per_minute: int, log_interval: float = 60.0):
        self.name = name
        self.cap = cap_per_minute
        self.log_interval = log_interval
        self._entries: deque[tuple[float, int]] = deque()
        self._lock = asyncio.Lock()
        self._stats = _BudgetStats()

    def _prune(self, now: float) -> int:
        cutoff = now - WINDOW_SECONDS
        while self._entries and self._entries[0][0] < cutoff:
            self._entries.popleft()
        return sum(w for _, w in self._entries)

    async def reserve(self, endpoint: str, weight: int) -> None:
        """Block (asyncio.sleep) until `weight` fits inside the rolling window."""
        if weight <= 0:
            return

        wait_total = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                current = self._prune(now)
                if current + weight <= self.cap:
                    self._entries.append((now, weight))
                    self._stats.reservations += 1
                    self._stats.total_weight += weight
                    self._stats.total_wait_seconds += wait_total
                    if wait_total > self._stats.max_wait_seconds:
                        self._stats.max_wait_seconds = wait_total
                    self._maybe_log(now, current + weight)
                    return

                oldest_ts, oldest_weight = self._entries[0]
                # Slightly overshoot so the oldest entry definitely ages out.
                sleep_for = (oldest_ts + WINDOW_SECONDS) - now + 0.05
                sleep_for = max(0.05, sleep_for)

            logger.debug(
                "WeightBudget[%s]: %s wants %d, current=%d/%d, sleeping %.2fs",
                self.name, endpoint, weight, current, self.cap, sleep_for,
            )
            await asyncio.sleep(sleep_for)
            wait_total += sleep_for

    def _maybe_log(self, now: float, current: int) -> None:
        if now - self._stats.last_log_time < self.log_interval:
            return
        logger.info(
            "WeightBudget[%s]: %d reservations, %d weight/60s (%.0f%% of cap), "
            "wait_total=%.1fs, wait_max=%.2fs",
            self.name, self._stats.reservations, current,
            100.0 * current / self.cap if self.cap else 0.0,
            self._stats.total_wait_seconds, self._stats.max_wait_seconds,
        )
        self._stats.last_log_time = now
        self._stats.total_wait_seconds = 0.0
        self._stats.max_wait_seconds = 0.0
        self._stats.reservations = 0

    def current_usage(self) -> int:
        """Return weight used in the current 60s window. Non-blocking snapshot."""
        return self._prune(time.monotonic())


class NoopBudget:
    """No-op budget for tests. Same interface, no waiting."""

    name = "noop"
    cap = 0

    async def reserve(self, endpoint: str, weight: int) -> None:
        return

    def current_usage(self) -> int:
        return 0


HL_BUDGET = WeightBudget("hyperliquid", cap_per_minute=1200)
