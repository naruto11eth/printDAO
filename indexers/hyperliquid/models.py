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
class Position:
    """A user's position on Hyperliquid."""
    coin: str
    size: Decimal
    entry_px: Decimal
    liquidation_px: Optional[Decimal]
    unrealized_pnl: Decimal
    leverage: Optional[Decimal]
    margin_used: Decimal
    position_value: Decimal
    return_on_equity: Decimal

    @classmethod
    def from_raw(cls, data: dict) -> "Position":
        """Create Position from user_state API response."""
        pos = data.get("position", data)
        return cls(
            coin=pos.get("coin", ""),
            size=Decimal(str(pos.get("szi", "0"))),
            entry_px=Decimal(str(pos.get("entryPx", "0"))) if pos.get("entryPx") else Decimal("0"),
            liquidation_px=Decimal(str(pos.get("liquidationPx"))) if pos.get("liquidationPx") else None,
            unrealized_pnl=Decimal(str(pos.get("unrealizedPnl", "0"))),
            leverage=Decimal(str(pos.get("leverage", {}).get("value", "0"))) if pos.get("leverage") else None,
            margin_used=Decimal(str(pos.get("marginUsed", "0"))),
            position_value=Decimal(str(pos.get("positionValue", "0"))),
            return_on_equity=Decimal(str(pos.get("returnOnEquity", "0"))),
        )

    @property
    def direction(self) -> Direction:
        """Get position direction."""
        return Direction.LONG if self.size > 0 else Direction.SHORT

    @property
    def abs_size(self) -> Decimal:
        """Get absolute position size."""
        return abs(self.size)

    @property
    def is_profitable(self) -> bool:
        """Check if position is profitable."""
        return self.unrealized_pnl > 0


@dataclass
class Fill:
    """A trade fill for a user."""
    coin: str
    side: TradeSide
    size: Decimal
    price: Decimal
    time_ms: int
    closed_pnl: Decimal
    hash: str
    fee: Decimal
    start_position: Decimal
    direction: str  # "Open Long", "Close Short", etc.

    @classmethod
    def from_raw(cls, data: dict) -> "Fill":
        """Create Fill from user_fills API response."""
        return cls(
            coin=data.get("coin", ""),
            side=TradeSide(data.get("side", "B")),
            size=Decimal(str(data.get("sz", "0"))),
            price=Decimal(str(data.get("px", "0"))),
            time_ms=int(data.get("time", 0)),
            closed_pnl=Decimal(str(data.get("closedPnl", "0"))),
            hash=data.get("hash", ""),
            fee=Decimal(str(data.get("fee", "0"))),
            start_position=Decimal(str(data.get("startPosition", "0"))),
            direction=data.get("dir", ""),
        )

    @property
    def datetime(self) -> datetime:
        """Get fill datetime."""
        return datetime.fromtimestamp(self.time_ms / 1000)

    @property
    def notional_usd(self) -> Decimal:
        """Calculate notional value."""
        return self.price * self.size


@dataclass