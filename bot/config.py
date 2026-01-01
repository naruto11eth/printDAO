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

    # Logging
    log_level: str = "INFO"

    # Forum topics (will be populated dynamically per chat)
    default_topics: Dict[str, TopicConfig] = field(default_factory=dict)
