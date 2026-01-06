"""Bot services module."""

from .notification_service import NotificationService
from .wallet_service import WalletService
from .position_indexer import PositionIndexer, TraderMetrics, PositionSnapshot
from .whale_detector import ConfigurableWhaleDetector, WhaleConfig, WalletProfile
from .analytics_service import AnalyticsService, TraderAnalytics, format_trader_analytics
from .copy_trading_service import (
    CopyTradingService,
    CopyTraderConfig,
    format_copy_signal,
    format_signal_history,
)

__all__ = [
    "NotificationService",
    "WalletService",
    "PositionIndexer",
    "TraderMetrics",
    "PositionSnapshot",
    "ConfigurableWhaleDetector",
    "WhaleConfig",
    "WalletProfile",
    "AnalyticsService",
    "TraderAnalytics",
    "format_trader_analytics",
    "CopyTradingService",
    "CopyTraderConfig",
    "format_copy_signal",
    "format_signal_history",
]
