"""Configurable whale detection service."""

import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Set

from bot.storage.redis_client import RedisClient

logger = logging.getLogger(__name__)


@dataclass
class WhaleConfig:
    """Configuration for whale detection."""
    # Trade size thresholds (USD)
    hyperliquid_threshold: float = 10_000_000  # $10M for perps
    polymarket_threshold: float = 10_000  # $10k for prediction markets

    # New wallet detection
    new_wallet_detection_enabled: bool = True
    new_wallet_first_trade_threshold: float = 50_000  # $50k first trade
    new_wallet_age_hours: int = 24  # Wallet seen less than 24h ago

    # Alert settings
    alert_cooldown_seconds: int = 300  # 5 min between alerts for same wallet

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WhaleConfig":
        """Create from dictionary."""
        return cls(**data)


@dataclass
class WalletProfile:
    """Profile of a tracked wallet."""
    address: str
    exchange: str
    first_seen_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)
    total_trades: int = 0
    total_volume: float = 0.0
    largest_trade: float = 0.0
    is_new_wallet: bool = True

    @property
    def age_hours(self) -> float:
        """Get wallet age in hours."""
        return (time.time() - self.first_seen_at) / 3600

    @property
    def age_days(self) -> int:
        """Get wallet age in days."""
        return int((time.time() - self.first_seen_at) / 86400)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WalletProfile":
        """Create from dictionary."""
        return cls(**data)


@dataclass
class WhaleAlert:
    """A whale alert to be sent."""
    wallet: str
    exchange: str
    alert_type: str  # "whale_trade", "new_whale", "first_large_trade"
    trade_value: float
    asset: Optional[str] = None
    market_title: Optional[str] = None
    side: Optional[str] = None
    price: Optional[float] = None
    size: Optional[float] = None
    wallet_profile: Optional[WalletProfile] = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        data = asdict(self)
        if self.wallet_profile:
            data["wallet_profile"] = self.wallet_profile.to_dict()
        return data


class ConfigurableWhaleDetector:
    """Configurable whale detection with new wallet tracking."""

    CONFIG_KEY = "whale_detector:config"
    PROFILE_KEY_PREFIX = "whale_detector:profile"
    ALERT_COOLDOWN_PREFIX = "whale_detector:cooldown"

    def __init__(
        self,
        redis_client: RedisClient,
        config: Optional[WhaleConfig] = None,
    ):
        """Initialize detector.

        Args:
            redis_client: Redis client for persistence
            config: Whale detection configuration
        """
        self.redis = redis_client
        self.config = config or WhaleConfig()
        # In-memory cache of profiles for faster lookups
        self._profile_cache: Dict[str, WalletProfile] = {}
        # Track alerted wallets to prevent spam
        self._recent_alerts: Set[str] = set()

    def _profile_key(self, exchange: str, address: str) -> str:
        """Get Redis key for wallet profile."""
        return f"{self.PROFILE_KEY_PREFIX}:{exchange}:{address.lower()}"

    def _cooldown_key(self, exchange: str, address: str) -> str:
        """Get Redis key for alert cooldown."""
        return f"{self.ALERT_COOLDOWN_PREFIX}:{exchange}:{address.lower()}"

    async def save_config(self) -> None:
        """Save current config to Redis."""
        await self.redis.set(self.CONFIG_KEY, self.config.to_dict())

    async def load_config(self) -> WhaleConfig:
        """Load config from Redis."""
        data = await self.redis.get(self.CONFIG_KEY)
        if data:
            self.config = WhaleConfig.from_dict(data)
        return self.config

    async def update_config(self, **kwargs) -> WhaleConfig:
        """Update specific config values.

        Args:
            **kwargs: Config fields to update

        Returns:
            Updated config
        """
        for key, value in kwargs.items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)
        await self.save_config()
        return self.config

    async def get_or_create_profile(
        self,
        address: str,
        exchange: str,
    ) -> WalletProfile:
        """Get existing profile or create new one.

        Args:
            address: Wallet address
            exchange: Exchange name

        Returns:
            WalletProfile
        """
        address_lower = address.lower()
        cache_key = f"{exchange}:{address_lower}"

        # Check cache first
        if cache_key in self._profile_cache:
            return self._profile_cache[cache_key]

        # Check Redis
        key = self._profile_key(exchange, address)
        data = await self.redis.get(key)

        if data:
            profile = WalletProfile.from_dict(data)
            profile.is_new_wallet = False
        else:
            # New wallet!
            profile = WalletProfile(
                address=address_lower,
                exchange=exchange,
                first_seen_at=time.time(),
                is_new_wallet=True,
            )

        self._profile_cache[cache_key] = profile
        return profile

    async def update_profile(
        self,
        address: str,
        exchange: str,
        trade_value: float,
    ) -> WalletProfile:
        """Update wallet profile with new trade.

        Args:
            address: Wallet address
            exchange: Exchange name
            trade_value: Trade value in USD

        Returns:
            Updated profile
        """
        profile = await self.get_or_create_profile(address, exchange)

        profile.last_seen_at = time.time()
        profile.total_trades += 1
        profile.total_volume += trade_value
        if trade_value > profile.largest_trade:
            profile.largest_trade = trade_value

        # Save to Redis
        key = self._profile_key(exchange, address)
        await self.redis.set(key, profile.to_dict())

        # Update cache
        cache_key = f"{exchange}:{address.lower()}"
        self._profile_cache[cache_key] = profile

        return profile

    async def check_alert_cooldown(
        self,
        address: str,
        exchange: str,
    ) -> bool:
        """Check if wallet is in alert cooldown.

        Args:
            address: Wallet address
            exchange: Exchange name

        Returns:
            True if in cooldown (should NOT alert)
        """
        key = self._cooldown_key(exchange, address)
        return await self.redis.exists(key)

    async def set_alert_cooldown(
        self,
        address: str,
        exchange: str,
    ) -> None:
        """Set alert cooldown for wallet.

        Args:
            address: Wallet address
            exchange: Exchange name
        """
        key = self._cooldown_key(exchange, address)
        await self.redis.set(key, "1", ex=self.config.alert_cooldown_seconds)

    async def should_alert(
        self,
        address: str,
        exchange: str,
        trade_value: float,
    ) -> Optional[WhaleAlert]:
        """Determine if a trade should trigger an alert.

        Args:
            address: Wallet address
            exchange: Exchange name
            trade_value: Trade value in USD

        Returns:
            WhaleAlert if should alert, None otherwise
        """
        # Check cooldown
        if await self.check_alert_cooldown(address, exchange):
            return None

        # Get threshold for this exchange
        threshold = (
            self.config.hyperliquid_threshold
            if exchange == "hyperliquid"
            else self.config.polymarket_threshold
        )

        # Get or create profile
        profile = await self.get_or_create_profile(address, exchange)

        alert_type = None

        # Check for new wallet with large first trade
        if (
            self.config.new_wallet_detection_enabled
            and profile.is_new_wallet
            and trade_value >= self.config.new_wallet_first_trade_threshold
        ):
            alert_type = "new_whale"

        # Check for regular whale trade
        elif trade_value >= threshold:
            # Check if this is first large trade from new-ish wallet
            if (
                self.config.new_wallet_detection_enabled
                and profile.age_hours < self.config.new_wallet_age_hours
                and profile.total_trades <= 1
                and trade_value >= self.config.new_wallet_first_trade_threshold
            ):
                alert_type = "first_large_trade"
            else:
                alert_type = "whale_trade"

        if alert_type:
            # Update profile with this trade
            await self.update_profile(address, exchange, trade_value)

            # Set cooldown
            await self.set_alert_cooldown(address, exchange)

            return WhaleAlert(
                wallet=address,
                exchange=exchange,
                alert_type=alert_type,
                trade_value=trade_value,
                wallet_profile=profile,
            )

        return None

    async def process_trade(
        self,
        address: str,
        exchange: str,
        trade_value: float,
        asset: Optional[str] = None,
        market_title: Optional[str] = None,
        side: Optional[str] = None,
        price: Optional[float] = None,
        size: Optional[float] = None,
    ) -> Optional[WhaleAlert]:
        """Process a trade and return alert if needed.

        Args:
            address: Wallet address
            exchange: Exchange name
            trade_value: Trade value in USD
            asset: Asset being traded
            market_title: Market title (for prediction markets)
            side: Trade side (BUY/SELL, LONG/SHORT)
            price: Trade price
            size: Trade size

        Returns:
            WhaleAlert if should alert, None otherwise
        """
        alert = await self.should_alert(address, exchange, trade_value)

        if alert:
            alert.asset = asset
            alert.market_title = market_title
            alert.side = side
            alert.price = price
            alert.size = size

        return alert

    async def get_new_whales(
        self,
        exchange: str,
        hours: int = 24,
        min_volume: float = 100_000,
    ) -> List[WalletProfile]:
        """Get recently discovered whales.

        Args:
            exchange: Exchange to filter
            hours: Max age in hours
            min_volume: Minimum total volume

        Returns:
            List of new whale profiles
        """
        # This would require scanning Redis keys
        # For now, return from cache
        cutoff = time.time() - (hours * 3600)
        new_whales = []

        for key, profile in self._profile_cache.items():
            if not key.startswith(f"{exchange}:"):
                continue
            if profile.first_seen_at >= cutoff and profile.total_volume >= min_volume:
                new_whales.append(profile)

        return sorted(new_whales, key=lambda p: p.total_volume, reverse=True)

    async def get_whale_leaderboard(
        self,
        exchange: str,
        limit: int = 10,
    ) -> List[WalletProfile]:
        """Get top whales by volume.

        Args:
            exchange: Exchange to filter
            limit: Max whales to return

        Returns:
            List of whale profiles sorted by volume
        """
        profiles = [
            p for key, p in self._profile_cache.items()
            if key.startswith(f"{exchange}:")
        ]

        return sorted(profiles, key=lambda p: p.total_volume, reverse=True)[:limit]
