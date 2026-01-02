"""Polymarket indexer - main service for tracking wallets and detecting whales."""

import asyncio
import logging
from typing import Optional, Callable, Awaitable
from datetime import datetime

from bot.storage.redis_client import RedisClient
from bot.storage.models import WhaleAlert, TrackedWallet
from bot.services.notification_service import NotificationService
from shared.constants import REDIS_KEYS

from .client import PolymarketClient
from .tracker import PolymarketTracker, WhaleDetector
from .models import Trade, Activity, Position

logger = logging.getLogger(__name__)


class PolymarketIndexer:
    """Main indexer service for Polymarket."""

    def __init__(
        self,
        redis_client: RedisClient,
        notification_service: NotificationService,
        whale_threshold: float = 10000.0,
        poll_interval: float = 30.0,
    ):
        """Initialize the indexer.

        Args:
            redis_client: Redis client for state management
            notification_service: Service for sending alerts
            whale_threshold: Minimum trade size for whale alerts (USD)
            poll_interval: Seconds between polling cycles
        """
        self.redis = redis_client
        self.notifications = notification_service
        self.whale_threshold = whale_threshold
        self.poll_interval = poll_interval

        # Initialize API client and trackers
        self.client = PolymarketClient(calls_per_second=5.0)
        self.tracker = PolymarketTracker(self.client, whale_threshold)
        self.whale_detector = WhaleDetector(self.client, whale_threshold)

        self._running = False
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        """Start the indexer."""
        logger.info("Starting Polymarket indexer...")
        self._running = True

        # Start background tasks
        self._tasks = [
            asyncio.create_task(self._poll_tracked_wallets()),
            asyncio.create_task(self._scan_whale_activity()),
        ]

        logger.info(f"Polymarket indexer started (whale threshold: ${self.whale_threshold:,.0f})")

    async def stop(self) -> None:
        """Stop the indexer."""
        logger.info("Stopping Polymarket indexer...")
        self._running = False
        self.tracker.stop_watching()
        self.whale_detector.stop()

        # Cancel all tasks
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await self.client.close()
        logger.info("Polymarket indexer stopped.")
