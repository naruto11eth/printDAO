"""Tests for the rolling-window HL rate-limit budget."""

import asyncio
import time

import pytest

from shared.weight_budget import WeightBudget, NoopBudget, WINDOW_SECONDS


class TestWeightBudget:
    async def test_under_cap_does_not_wait(self):
        budget = WeightBudget("t", cap_per_minute=100)
        t0 = time.monotonic()
        await budget.reserve("x", 20)
        await budget.reserve("x", 20)
        await budget.reserve("x", 20)
        assert time.monotonic() - t0 < 0.05
        assert budget.current_usage() == 60

    async def test_at_cap_blocks_until_window_rolls(self):
        budget = WeightBudget("t", cap_per_minute=100)
        # Fill the window.
        await budget.reserve("x", 100)
        # Prime the deque with an "old" entry by patching the first entry's timestamp.
        # Shift the only entry back so only ~0.2s remains before it ages out.
        ts, w = budget._entries[0]
        budget._entries[0] = (ts - WINDOW_SECONDS + 0.2, w)

        t0 = time.monotonic()
        await budget.reserve("x", 10)
        elapsed = time.monotonic() - t0
        # Must have waited ~0.2s (+0.05 overshoot margin), well below WINDOW_SECONDS.
        assert 0.15 <= elapsed < 1.0

    async def test_prunes_old_entries(self):
        budget = WeightBudget("t", cap_per_minute=100)
        await budget.reserve("x", 50)
        # Age the entry outside the window.
        ts, w = budget._entries[0]
        budget._entries[0] = (ts - WINDOW_SECONDS - 1, w)
        assert budget.current_usage() == 0

    async def test_zero_weight_skip(self):
        budget = WeightBudget("t", cap_per_minute=100)
        await budget.reserve("x", 0)
        assert budget.current_usage() == 0

    async def test_parallel_reservations_respect_cap(self):
        """Two coroutines racing — total usage must stay ≤ cap at any moment."""
        budget = WeightBudget("t", cap_per_minute=40)

        async def take(n: int):
            await budget.reserve("x", n)

        # Total 40 weight in 4 concurrent requests. Should serialize within cap.
        await asyncio.gather(take(10), take(10), take(10), take(10))
        assert budget.current_usage() == 40

    async def test_noop_budget(self):
        budget = NoopBudget()
        t0 = time.monotonic()
        for _ in range(100):
            await budget.reserve("x", 1000)
        assert time.monotonic() - t0 < 0.05
        assert budget.current_usage() == 0
