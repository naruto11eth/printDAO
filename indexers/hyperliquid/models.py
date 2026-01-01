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