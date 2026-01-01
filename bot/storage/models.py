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
