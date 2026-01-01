"""Hyperliquid data models."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional
from enum import Enum


class TradeSide(str, Enum):
    """Trade side. B=Bid/Buy, A=Ask/Sell."""
    BUY = "B"
    SELL = "A"  # Ask = Sell


class Direction(str, Enum):
    """Position direction."""
    LONG = "long"
    SHORT = "short"


@dataclass(frozen=True)
class Trade:
    """A trade on Hyperliquid."""
    coin: str
    tid: str  # Trade ID
    time_ms: int
    price: Decimal
    size: Decimal
    side: TradeSide
    hash: str
    users: tuple[str, str]  # (taker, maker)

    @classmethod
    def from_raw(cls, coin: str, data: dict) -> "Trade":
        """Create Trade from API response."""
        return cls(
            coin=coin,
            tid=str(data.get("tid", "")),
            time_ms=int(data.get("time", 0)),
            price=Decimal(str(data.get("px", "0"))),
            size=Decimal(str(data.get("sz", "0"))),
            side=TradeSide(data.get("side", "B")),
            hash=data.get("hash", ""),
            users=(
                data.get("users", ["", ""])[0] if data.get("users") else "",
                data.get("users", ["", ""])[1] if data.get("users") and len(data.get("users", [])) > 1 else "",
            ),
        )

    @property
    def datetime(self) -> datetime:
        """Get trade datetime."""
        return datetime.fromtimestamp(self.time_ms / 1000)

    @property
    def notional_usd(self) -> Decimal:
        """Calculate notional value in USD."""
        return self.price * self.size

    @property
    def direction(self) -> Direction:
        """Get trade direction (long/short)."""
        return Direction.LONG if self.side.value == "B" else Direction.SHORT

    @property
    def is_whale(self) -> bool:
        """Check if this is a whale trade ($10M+)."""
        return self.notional_usd >= Decimal("10000000")


@dataclass