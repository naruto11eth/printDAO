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

    async def track_wallet(
        self,
        user_id: int,
        address: str,
        label: Optional[str] = None,
        exchange: str = "all",
        group_id: Optional[str] = None,
    ) -> ServiceResult:
        """Start tracking a wallet.

        Args:
            user_id: Telegram user ID
            address: Wallet address to track
            label: Optional friendly name
            exchange: Exchange filter ("hyperliquid", "polymarket", or "all")
            group_id: Optional group to add to

        Returns:
            ServiceResult with success/error
        """
        # Validate address
        if not is_valid_ethereum_address(address):
            return ServiceResult.fail(
                f"Invalid wallet address format. Must be 0x followed by 40 hex characters."
            )

        # Validate exchange
        if exchange != "all" and exchange not in EXCHANGES:
            return ServiceResult.fail(
                f"Invalid exchange. Must be one of: {', '.join(EXCHANGES)}, or 'all'"
            )

        address = normalize_address(address)
        user_key = self._user_wallets_key(user_id)

        # Check if already tracking
        existing = await self.redis.hget(user_key, address)
        if existing:
            return ServiceResult.fail(
                f"Already tracking this wallet. Use /untrack to stop tracking first."
            )

        # Check wallet limit
        current_count = await self.redis.hlen(user_key)
        if current_count >= self.max_wallets:
            return ServiceResult.fail(
                f"Maximum wallet limit reached ({self.max_wallets}). "
                f"Use /untrack to remove some wallets first."
            )

        # Create tracked wallet
        wallet = TrackedWallet(
            address=address,
            label=label,
            exchange=exchange,
            group_id=group_id,
        )

        # Save to user's wallet list
        await self.redis.hset(user_key, address, wallet.to_dict())

        # Add user to wallet's subscriber list (for notifications)
        await self.redis.sadd(self._wallet_subscribers_key(address), str(user_id))

        logger.info(f"User {user_id} started tracking wallet {address}")
        return ServiceResult.ok(wallet)

    async def untrack_wallet(
        self,
        user_id: int,
        identifier: str,
    ) -> ServiceResult:
        """Stop tracking a wallet.

        Args:
            user_id: Telegram user ID
            identifier: Wallet address or label

        Returns:
            ServiceResult with success/error
        """
        user_key = self._user_wallets_key(user_id)

        # Try to find by address first
        address = normalize_address(identifier)
        wallet_data = await self.redis.hget(user_key, address)

        if not wallet_data:
            # Try to find by label
            all_wallets = await self.redis.hgetall(user_key)
            for addr, data in all_wallets.items():
                wallet = TrackedWallet.from_dict(data)
                if wallet.label and wallet.label.lower() == identifier.lower():
                    address = addr
                    wallet_data = data
                    break

        if not wallet_data:
            return ServiceResult.fail(
                f"Wallet not found. Use /list to see your tracked wallets."
            )

        # Remove from user's list
        await self.redis.hdel(user_key, address)

        # Remove user from wallet's subscriber list
        await self.redis.srem(self._wallet_subscribers_key(address), str(user_id))

        logger.info(f"User {user_id} stopped tracking wallet {address}")
        return ServiceResult.ok(TrackedWallet.from_dict(wallet_data))

    async def get_tracked_wallets(
        self,
        user_id: int,
        exchange: Optional[str] = None,
        group_id: Optional[str] = None,
    ) -> List[TrackedWallet]:
        """Get all wallets tracked by a user.

        Args:
            user_id: Telegram user ID
            exchange: Optional exchange filter
            group_id: Optional group filter

        Returns:
            List of TrackedWallet objects
        """
        user_key = self._user_wallets_key(user_id)
        all_data = await self.redis.hgetall(user_key)

        wallets = []
        for data in all_data.values():
            wallet = TrackedWallet.from_dict(data)

            # Apply filters
            if exchange and wallet.exchange != "all" and wallet.exchange != exchange:
                continue
            if group_id and wallet.group_id != group_id:
                continue

            wallets.append(wallet)

        # Sort by added_at (most recent first)
        wallets.sort(key=lambda w: w.added_at, reverse=True)
        return wallets

    async def get_wallet_subscribers(self, address: str) -> List[int]:
        """Get all users tracking a specific wallet.

        Args:
            address: Wallet address

        Returns:
            List of user IDs
        """
        address = normalize_address(address)
        subscribers = await self.redis.smembers(self._wallet_subscribers_key(address))
        return [int(s) for s in subscribers]

    async def is_tracking(self, user_id: int, address: str) -> bool:
        """Check if user is tracking a wallet.

        Args:
            user_id: Telegram user ID
            address: Wallet address

        Returns:
            True if tracking
        """
        address = normalize_address(address)
        return await self.redis.hexists(self._user_wallets_key(user_id), address)

    # --- Wallet Groups ---
