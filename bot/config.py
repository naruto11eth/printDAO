"""Configuration management for PrintDAO bot."""

import os
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Load environment variables from .env file
load_dotenv()


@dataclass
class TopicConfig:
    """Configuration for a forum topic."""
    name: str
    topic_id: Optional[int] = None  # Set dynamically when topics are created
    description: str = ""


@dataclass
class BotConfig:
    """Main bot configuration."""

    # Telegram
    bot_token: str = ""

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Thresholds (in USD)
    hyperliquid_whale_threshold: int = 10_000_000  # $10M
    polymarket_whale_threshold: int = 10_000       # $10k

    # User limits
    max_tracked_wallets_per_user: int = 20
    max_groups_per_user: int = 10

    # Admin
    admin_user_ids: set[int] = field(default_factory=set)

    # Logging
    log_level: str = "INFO"

    # Forum topics (will be populated dynamically per chat)
    default_topics: Dict[str, TopicConfig] = field(default_factory=dict)

    def __post_init__(self):
        """Set up default topics if not provided."""
        if not self.default_topics:
            self.default_topics = {
                "hyperliquid": TopicConfig(
                    name="Hyperliquid",
                    description="Hyperliquid perpetual futures whale alerts"
                ),
                "polymarket": TopicConfig(
                    name="Polymarket",
                    description="Polymarket prediction market whale alerts"
                ),
                "whale_alerts": TopicConfig(
                    name="Whale Alerts",
                    description="New whale discoveries across all exchanges"
                ),
                "copy_trading": TopicConfig(
                    name="Copy Trading",
                    description="Copy trading signals from tracked wallets"
                ),
                "leaderboards": TopicConfig(
                    name="Leaderboards",
                    description="Daily/weekly PnL rankings"
                ),
            }

    @classmethod
    def from_env(cls) -> "BotConfig":
        """Create config from environment variables."""
        return cls(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            hyperliquid_whale_threshold=int(
                os.getenv("HYPERLIQUID_WHALE_THRESHOLD", "10000000")
            ),
            polymarket_whale_threshold=int(
                os.getenv("POLYMARKET_WHALE_THRESHOLD", "10000")
            ),
            max_tracked_wallets_per_user=int(
                os.getenv("MAX_TRACKED_WALLETS_PER_USER", "20")
            ),
            admin_user_ids=cls._parse_admin_ids(os.getenv("ADMIN_USER_IDS", "")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )

    @staticmethod
    def _parse_admin_ids(raw: str) -> set[int]:
        """Parse comma-separated admin user IDs from env var."""
        if not raw.strip():
            return set()
        ids = set()
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                ids.add(int(part))
        return ids

    def validate(self) -> bool:
        """Validate that required config is present."""
        if not self.bot_token:
            logger.error("TELEGRAM_BOT_TOKEN is required")
            return False
        return True


def setup_logging(level: str = "INFO") -> None:
    """Configure logging for the application."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Reduce noise from third-party libraries
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
