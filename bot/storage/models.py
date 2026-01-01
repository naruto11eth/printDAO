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