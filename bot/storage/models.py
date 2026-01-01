"""Data models for PrintDAO."""

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional
import time
import uuid


class NotificationPreference(str, Enum):
    """User notification preferences."""
    ALL = "all"
    WHALE_ONLY = "whale_only"
    SUMMARY = "summary"
    NONE = "none"


@dataclass
class TrackedWallet:
    """A wallet being tracked by a user."""
    address: str
    label: Optional[str] = None
    exchange: str = "all"  # "hyperliquid", "polymarket", or "all"
    group_id: Optional[str] = None
    added_at: float = field(default_factory=time.time)
    alert_threshold: Optional[int] = None  # Custom threshold for this wallet

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TrackedWallet":
        """Create from dictionary."""
        return cls(**data)


@dataclass
class WalletGroup:
    """A named group of tracked wallets."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    description: Optional[str] = None
    wallet_addresses: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    notification_preference: NotificationPreference = NotificationPreference.ALL

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        data = asdict(self)
        data["notification_preference"] = self.notification_preference.value
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WalletGroup":
        """Create from dictionary."""
        if "notification_preference" in data:
            data["notification_preference"] = NotificationPreference(
                data["notification_preference"]
            )
        return cls(**data)


@dataclass
class WhaleInfo:
    """Information about a discovered whale."""
    address: str
    exchange: str
    first_seen: float = field(default_factory=time.time)
    total_volume: float = 0.0
    trade_count: int = 0
    last_active: float = field(default_factory=time.time)
    pnl_all_time: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WhaleInfo":
        """Create from dictionary."""
        return cls(**data)


@dataclass
class WhaleAlert:
    """An alert about whale activity."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    alert_type: str = ""  # From AlertType constants
    wallet: str = ""
    exchange: str = ""
    trade_value: float = 0.0
    side: Optional[str] = None  # BUY/SELL or LONG/SHORT
    asset: Optional[str] = None
    market_title: Optional[str] = None
    price: Optional[float] = None
    size: Optional[float] = None
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)


@dataclass
class CopyTradeSignal:
    """A copy trading signal when tracked wallet trades."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    source_wallet: str = ""
    source_label: Optional[str] = None
    action: str = ""  # LONG, SHORT, BUY_YES, etc.
    exchange: str = ""
    asset: str = ""
    amount: float = 0.0
    price: float = 0.0
    notional_usd: float = 0.0
    leverage: Optional[int] = None  # For perps
    outcome: Optional[str] = None  # For prediction markets (YES/NO)
    market_title: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)


@dataclass