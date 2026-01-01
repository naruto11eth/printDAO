"""Polymarket data models."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from enum import Enum


class ActivityType(str, Enum):
    """Polymarket activity types."""
    TRADE = "TRADE"
    SPLIT = "SPLIT"
    MERGE = "MERGE"
    REDEEM = "REDEEM"
    REWARD = "REWARD"
    CONVERSION = "CONVERSION"


class TradeSide(str, Enum):
    """Trade side."""
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class Position:
    """A user's position in a Polymarket market."""
    proxy_wallet: str
    condition_id: str
    asset: str
    size: float
    avg_price: float
    initial_value: float
    current_value: float
    cash_pnl: float
    percent_pnl: float
    total_bought: float
    realized_pnl: float
    percent_realized_pnl: float
    cur_price: float
    redeemable: bool
    mergeable: bool
    title: str
    slug: str
    icon: Optional[str]
    event_slug: str
    outcome: str
    outcome_index: int
    opposite_outcome: Optional[str]
    opposite_asset: Optional[str]
    end_date: Optional[str]
    negative_risk: bool

    @classmethod