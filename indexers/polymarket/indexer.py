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

    async def _poll_tracked_wallets(self) -> None:
        """Poll tracked wallets for new activity."""
        while self._running:
            try:
                # Get all tracked wallets for Polymarket
                pattern = REDIS_KEYS["user_wallets"].format(user_id="*")
                keys = await self.redis.redis.keys(pattern)

                for key in keys:
                    # Get wallet data
                    wallets_data = await self.redis.redis.hgetall(key)

                    for wallet_key, wallet_json in wallets_data.items():
                        try:
                            wallet = TrackedWallet.from_json(wallet_json)

                            # Only process Polymarket or "all" exchange wallets
                            if wallet.exchange not in ("polymarket", "all"):
                                continue

                            # Check for new trades
                            await self._check_wallet_activity(wallet)

                        except Exception as e:
                            logger.error(f"Error processing wallet {wallet_key}: {e}")

                await asyncio.sleep(self.poll_interval)

            except Exception as e:
                logger.error(f"Error in wallet polling loop: {e}")
                await asyncio.sleep(self.poll_interval)

    async def _check_wallet_activity(self, wallet: TrackedWallet) -> None:
        """Check a wallet for new trading activity.

        Args:
            wallet: Tracked wallet to check
        """
        # Get last check timestamp from Redis
        last_check_key = f"pm:last_check:{wallet.address}"
        last_check = await self.redis.redis.get(last_check_key)
        last_timestamp = int(last_check) if last_check else None

        # Get recent trades
        trades = await self.tracker.detect_whale_trades(
            address=wallet.address,
            since_timestamp=last_timestamp,
        )

        # Send alerts for whale trades
        for trade in trades:
            await self._send_trade_alert(wallet, trade)

        # Update last check timestamp
        await self.redis.redis.set(last_check_key, str(int(datetime.now().timestamp())))

    async def _send_trade_alert(self, wallet: TrackedWallet, trade: Activity) -> None:
        """Send alert for a tracked wallet trade.

        Args:
            wallet: The tracked wallet
            trade: The trade activity
        """
        # Create whale alert
        alert = WhaleAlert(
            exchange="polymarket",
            address=wallet.address,
            trade_type=trade.side.value if trade.side else "TRADE",
            size=trade.usdc_size,
            market=trade.title,
            price=trade.price,
            timestamp=datetime.fromtimestamp(trade.timestamp),
        )

        # Format message
        side_emoji = "🟢" if trade.side and trade.side.value == "BUY" else "🔴"
        label_str = f" ({wallet.label})" if wallet.label else ""

        message = (
            f"{side_emoji} <b>Tracked Wallet Trade</b>\n\n"
            f"<b>Wallet:</b> <code>{wallet.address[:10]}...{wallet.address[-6:]}</code>{label_str}\n"
            f"<b>Market:</b> {trade.title}\n"
            f"<b>Position:</b> {trade.outcome}\n"
            f"<b>Side:</b> {trade.side.value if trade.side else 'TRADE'}\n"
            f"<b>Size:</b> ${trade.usdc_size:,.2f}\n"
        )

        if trade.price:
            message += f"<b>Price:</b> {trade.price:.2f}%\n"

        message += (
            f"\n<a href=\"https://polymarket.com/event/{trade.event_slug}\">View Market</a> | "
            f"<a href=\"https://polygonscan.com/tx/{trade.transaction_hash}\">View Tx</a>"
        )

        # Send to user who's tracking this wallet
        await self.notifications.send_whale_alert(
            chat_id=wallet.user_id,
            alert=alert,
            custom_message=message,
        )

    async def _scan_whale_activity(self) -> None:
        """Scan for general whale activity across Polymarket."""
        while self._running:
            try:
                # Get recent whale trades
                trades = await self.tracker.get_recent_whale_trades(limit=20)

                for trade in trades:
                    # Check if we've already processed this trade
                    processed_key = f"pm:processed:{trade.transaction_hash}"
                    if await self.redis.redis.exists(processed_key):
                        continue

                    # Mark as processed (expire after 24 hours)
                    await self.redis.redis.setex(processed_key, 86400, "1")

                    # Broadcast whale alert to all subscribers
                    await self._broadcast_whale_alert(trade)

                await asyncio.sleep(self.poll_interval)

            except Exception as e:
                logger.error(f"Error in whale scanning loop: {e}")
                await asyncio.sleep(self.poll_interval)

    async def _broadcast_whale_alert(self, trade: Trade) -> None:
        """Broadcast whale alert to all subscribed chats.

        Args:
            trade: The whale trade
        """
        # Get all chats subscribed to whale alerts
        subscribers_key = "pm:whale_subscribers"
        subscribers = await self.redis.redis.smembers(subscribers_key)

        if not subscribers:
            return

        # Create alert
        alert = WhaleAlert(
            exchange="polymarket",
            address=trade.proxy_wallet,
            trade_type=trade.side.value,
            size=trade.usdc_size,
            market=trade.title,
            price=trade.price,
            timestamp=datetime.fromtimestamp(trade.timestamp),
        )

        # Format message
        side_emoji = "🟢" if trade.side.value == "BUY" else "🔴"
        trader_name = trade.pseudonym or trade.name or f"{trade.proxy_wallet[:10]}..."

        message = (
            f"🐋 <b>Polymarket Whale Alert</b>\n\n"
            f"{side_emoji} <b>{trader_name}</b>\n"
            f"<b>Market:</b> {trade.title}\n"
            f"<b>Position:</b> {trade.outcome}\n"
            f"<b>Size:</b> <b>${trade.usdc_size:,.0f}</b>\n"
            f"<b>Price:</b> {trade.price:.1f}%\n\n"
            f"<a href=\"https://polymarket.com/event/{trade.event_slug}\">View Market</a>"
        )

        # Send to all subscribers
        for chat_id in subscribers:
            try:
                await self.notifications.send_whale_alert(
                    chat_id=int(chat_id),
                    alert=alert,
                    custom_message=message,
                )
            except Exception as e:
                logger.error(f"Error sending whale alert to {chat_id}: {e}")
