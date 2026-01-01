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
    def from_api(cls, data: dict) -> "Position":
        """Create Position from API response."""
        return cls(
            proxy_wallet=data.get("proxyWallet", ""),
            condition_id=data.get("conditionId", ""),
            asset=data.get("asset", ""),
            size=float(data.get("size", 0)),
            avg_price=float(data.get("avgPrice", 0)),
            initial_value=float(data.get("initialValue", 0)),
            current_value=float(data.get("currentValue", 0)),
            cash_pnl=float(data.get("cashPnl", 0)),
            percent_pnl=float(data.get("percentPnl", 0)),
            total_bought=float(data.get("totalBought", 0)),
            realized_pnl=float(data.get("realizedPnl", 0)),
            percent_realized_pnl=float(data.get("percentRealizedPnl", 0)),
            cur_price=float(data.get("curPrice", 0)),
            redeemable=data.get("redeemable", False),
            mergeable=data.get("mergeable", False),
            title=data.get("title", ""),
            slug=data.get("slug", ""),
            icon=data.get("icon"),
            event_slug=data.get("eventSlug", ""),
            outcome=data.get("outcome", ""),
            outcome_index=int(data.get("outcomeIndex", 0)),
            opposite_outcome=data.get("oppositeOutcome"),
            opposite_asset=data.get("oppositeAsset"),
            end_date=data.get("endDate"),
            negative_risk=data.get("negativeRisk", False),
        )

    @property
    def potential_payout(self) -> float:
        """Calculate potential payout if position wins."""
        return self.size  # Each share pays $1 if correct

    @property
    def is_winning(self) -> bool:
        """Check if position is currently profitable."""
        return self.cash_pnl > 0


@dataclass
class Trade:
    """A trade on Polymarket."""
    proxy_wallet: str
    timestamp: int
    condition_id: str
    side: TradeSide
    asset: str
    size: float
    usdc_size: float
    price: float
    transaction_hash: str
    outcome_index: int
    title: str
    slug: str
    icon: Optional[str]
    event_slug: str
    outcome: str
    # Trader info
    name: Optional[str] = None
    pseudonym: Optional[str] = None
    bio: Optional[str] = None
    profile_image: Optional[str] = None

    @classmethod
    def from_api(cls, data: dict) -> "Trade":
        """Create Trade from API response."""
        side_str = data.get("side", "BUY")
        size = float(data.get("size", 0))
        price = float(data.get("price", 0))
        # Calculate USDC value: size * price for trades endpoint
        # The trades endpoint returns share count in 'size', not USDC
        usdc_size = data.get("usdcSize") or (size * price)
        return cls(
            proxy_wallet=data.get("proxyWallet", ""),
            timestamp=int(data.get("timestamp", 0)),
            condition_id=data.get("conditionId", ""),
            side=TradeSide(side_str) if side_str else TradeSide.BUY,
            asset=data.get("asset", ""),
            size=size,
            usdc_size=float(usdc_size) if usdc_size else size * price,
            price=price,
            transaction_hash=data.get("transactionHash", ""),
            outcome_index=int(data.get("outcomeIndex", 0)),
            title=data.get("title", ""),
            slug=data.get("slug", ""),
            icon=data.get("icon"),
            event_slug=data.get("eventSlug", ""),
            outcome=data.get("outcome", ""),
            name=data.get("name"),
            pseudonym=data.get("pseudonym"),
            bio=data.get("bio"),
            profile_image=data.get("profileImage"),
        )

    @property
    def datetime(self) -> datetime:
        """Get trade datetime."""
        return datetime.fromtimestamp(self.timestamp)

    @property
    def is_whale_trade(self) -> bool:
        """Check if this is a whale trade ($10k+)."""
        return self.usdc_size >= 10000


@dataclass