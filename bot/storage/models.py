"""Data models for PrintDAO."""

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional
import time
import uuid

from shared.models import safe_from_dict


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
        """Create from dictionary, filtering unknown fields."""
        return safe_from_dict(cls, data)


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
        """Create from dictionary, filtering unknown fields."""
        return safe_from_dict(cls, data)


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
class PnLResult:
    """PnL calculation result for a wallet."""
    wallet: str
    exchange: str
    period: str  # day, week, month, all
    cash_pnl: float = 0.0
    percent_pnl: float = 0.0
    portfolio_value: float = 0.0
    position_count: int = 0
    calculated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for caching."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PnLResult":
        """Create from dictionary, filtering unknown fields."""
        return safe_from_dict(cls, data)


@dataclass
class Position:
    """A trading position."""
    wallet: str
    exchange: str
    market_id: str
    asset: Optional[str] = None
    outcome: Optional[str] = None  # For prediction markets
    market_title: Optional[str] = None
    size: float = 0.0
    avg_price: float = 0.0
    current_price: float = 0.0
    current_value: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    leverage: Optional[int] = None  # For perps
    direction: Optional[str] = None  # long/short for perps
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)


@dataclass
class WhaleDetectionConfig:
    """User-configurable whale detection parameters."""

    min_trade_size_usd: float = 10_000
    new_wallet_first_trade_threshold: float = 50_000
    new_wallet_age_days: int = 7
    unusual_size_multiplier: float = 5.0
    alert_on_new_whales: bool = True
    alert_on_unusual_activity: bool = True
    alert_on_accumulation: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WhaleDetectionConfig":
        """Create from dictionary, filtering unknown fields."""
        return safe_from_dict(cls, data)


@dataclass
class UserPreferences:
    """User settings and preferences."""

    user_id: int = 0
    notification_preference: NotificationPreference = NotificationPreference.ALL
    whale_config: WhaleDetectionConfig = field(default_factory=WhaleDetectionConfig)
    monitoring_intervals: Dict[str, int] = field(default_factory=lambda: {
        "hyperliquid": 60,
        "polymarket": 300,
    })
    copy_trading_enabled: bool = False
    copy_trading_wallets: List[str] = field(default_factory=list)
    muted_until: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        data = asdict(self)
        data["notification_preference"] = self.notification_preference.value
        data["whale_config"] = self.whale_config.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UserPreferences":
        """Create from dictionary."""
        if "notification_preference" in data:
            data["notification_preference"] = NotificationPreference(
                data["notification_preference"]
            )
        if "whale_config" in data and isinstance(data["whale_config"], dict):
            data["whale_config"] = WhaleDetectionConfig.from_dict(data["whale_config"])
        return cls(**data)


@dataclass
class ServiceResult:
    """Generic result from service operations."""
    success: bool
    error: Optional[str] = None
    data: Optional[Any] = None

    @classmethod
    def ok(cls, data: Any = None) -> "ServiceResult":
        """Create a successful result."""
        return cls(success=True, data=data)

    @classmethod
    def fail(cls, error: str) -> "ServiceResult":
        """Create a failed result."""
        return cls(success=False, error=error)
