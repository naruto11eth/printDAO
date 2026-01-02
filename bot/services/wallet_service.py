"""Wallet tracking service for PrintDAO."""

import logging
from typing import List, Optional

from bot.storage.redis_client import RedisClient
from bot.storage.models import TrackedWallet, WalletGroup, ServiceResult
from shared.constants import (
    REDIS_KEYS,
    MAX_TRACKED_WALLETS_PER_USER,
    MAX_GROUPS_PER_USER,
    EXCHANGES,
)
from shared.utils import normalize_address, is_valid_ethereum_address

logger = logging.getLogger(__name__)


class WalletService:
    """Manages wallet tracking for users."""

    def __init__(self, redis: RedisClient, max_wallets: int = MAX_TRACKED_WALLETS_PER_USER):
        """Initialize wallet service.

        Args:
            redis: RedisClient instance
            max_wallets: Maximum wallets per user
        """
        self.redis = redis
        self.max_wallets = max_wallets

    def _user_wallets_key(self, user_id: int) -> str:
        """Get Redis key for user's wallets."""
        return REDIS_KEYS["user_wallets"].format(user_id=user_id)

    def _user_groups_key(self, user_id: int) -> str:
        """Get Redis key for user's groups."""
        return REDIS_KEYS["user_groups"].format(user_id=user_id)

    def _wallet_subscribers_key(self, address: str) -> str:
        """Get Redis key for wallet's subscribers."""
        return REDIS_KEYS["wallet_subscribers"].format(address=normalize_address(address))

    # --- Wallet Tracking ---
