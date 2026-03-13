"""Whale flow detection — detects coordinated whale activity on the same asset."""

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from bot.storage.redis_client import RedisClient

logger = logging.getLogger(__name__)


@dataclass
class WhaleFlowEvent:
    """A single whale trade contributing to a flow."""
    address: str
    exchange: str
    asset: str
    side: str
    trade_value: float
    timestamp: float
    trader_score: float = 0.0


@dataclass
class WhaleFlow:
    """Detected coordinated whale flow on an asset."""
    asset: str
    exchange: str
    side: str
    total_value: float
    whale_count: int
    events: list[WhaleFlowEvent] = field(default_factory=list)
    avg_trader_score: float = 0.0
    detected_at: float = field(default_factory=time.time)

    @property
    def conviction(self) -> str:
        if self.avg_trader_score >= 60 and self.whale_count >= 3:
            return "HIGH"
        if self.avg_trader_score >= 40 or self.whale_count >= 2:
            return "MEDIUM"
        return "LOW"


class WhaleFlowDetector:
    """Detects when multiple whales enter the same asset within a time window.

    Maintains a sliding window of recent whale trades. When 2+ whales trade
    the same asset on the same side within the window, a flow alert fires.
    """

    FLOW_KEY = "whale_flow:{exchange}:{asset}:{side}"

    def __init__(
        self,
        redis: RedisClient,
        window_seconds: int = 120,
        min_whales: int = 2,
    ):
        self.redis = redis
        self.window_seconds = window_seconds
        self.min_whales = min_whales
        self._recent_events: list[WhaleFlowEvent] = []
        self._max_events = 1000

    async def record_whale_trade(self, event: WhaleFlowEvent) -> WhaleFlow | None:
        """Record a whale trade and check if it creates a flow.

        Returns WhaleFlow if coordinated activity detected, None otherwise.
        """
        self._recent_events.append(event)

        # Prune old events
        cutoff = time.time() - self.window_seconds
        self._recent_events = [e for e in self._recent_events if e.timestamp > cutoff]

        # Cap in-memory list
        if len(self._recent_events) > self._max_events:
            self._recent_events = self._recent_events[-self._max_events:]

        # Check for flow on this asset + side
        matching = [
            e for e in self._recent_events
            if e.asset == event.asset
            and e.side == event.side
            and e.exchange == event.exchange
        ]

        # Deduplicate by address (same whale trading twice doesn't count as flow)
        unique_addresses = {e.address.lower() for e in matching}

        if len(unique_addresses) < self.min_whales:
            return None

        # Flow detected
        total_value = sum(e.trade_value for e in matching)
        scores = [e.trader_score for e in matching if e.trader_score > 0]
        avg_score = sum(scores) / len(scores) if scores else 0.0

        flow = WhaleFlow(
            asset=event.asset,
            exchange=event.exchange,
            side=event.side,
            total_value=total_value,
            whale_count=len(unique_addresses),
            events=matching,
            avg_trader_score=avg_score,
        )

        # Persist flow to Redis for querying
        await self._store_flow(flow)

        return flow

    async def _store_flow(self, flow: WhaleFlow) -> None:
        """Store detected flow in Redis."""
        key = f"whale_flow:{flow.exchange}:{flow.asset}:{flow.side}"
        flow_data = {
            "asset": flow.asset,
            "exchange": flow.exchange,
            "side": flow.side,
            "total_value": str(flow.total_value),
            "whale_count": str(flow.whale_count),
            "avg_score": str(flow.avg_trader_score),
            "conviction": flow.conviction,
            "detected_at": str(flow.detected_at),
        }
        await self.redis.set(key, flow_data, ex=self.window_seconds * 2)

    async def get_active_flows(self, exchange: str | None = None) -> list[dict[str, Any]]:
        """Get currently active whale flows."""
        pattern = f"whale_flow:{exchange or '*'}:*:*"
        keys = await self.redis.scan_keys(pattern)
        flows = []
        for key in keys:
            data = await self.redis.get(key)
            if data and isinstance(data, dict):
                flows.append(data)
        return sorted(flows, key=lambda f: float(f.get("total_value", 0)), reverse=True)


def format_whale_flow(flow: WhaleFlow) -> str:
    """Format a whale flow alert as HTML for Telegram."""
    side_emoji = "🟢" if flow.side.upper() in ("BUY", "LONG") else "🔴"
    conviction_emoji = {"HIGH": "🔥", "MEDIUM": "⚡", "LOW": "💧"}.get(flow.conviction, "")

    lines = [
        f"{side_emoji} <b>WHALE FLOW DETECTED</b> {conviction_emoji}",
        f"",
        f"<b>{flow.whale_count} whales</b> {flow.side.upper()} <b>{flow.asset}</b>",
        f"Total: <b>${flow.total_value:,.0f}</b>",
        f"Conviction: {flow.conviction}",
        f"Avg Score: {flow.avg_trader_score:.0f}/100",
    ]

    if flow.events:
        lines.append(f"\nTraders:")
        for e in flow.events[:5]:
            addr_short = f"{e.address[:6]}...{e.address[-4:]}"
            lines.append(f"  <code>{addr_short}</code> ${e.trade_value:,.0f}")

    return "\n".join(lines)
