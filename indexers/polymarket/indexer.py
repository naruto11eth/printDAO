"""Polymarket indexer - main service for tracking wallets and detecting whales."""

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional, Callable, Awaitable

from bot.storage.redis_client import RedisClient
from bot.storage.models import WhaleAlert, TrackedWallet
from bot.services.notification_service import NotificationService
from shared.constants import REDIS_KEYS
from shared.utils import escape_html, shorten_address

from .client import PolymarketClient
from .tracker import PolymarketTracker, WhaleDetector
from .models import Trade, Activity, Position

logger = logging.getLogger(__name__)

_USER_WALLETS_KEY_PATTERN = re.compile(r"user:(\d+):wallets")


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
                pattern = REDIS_KEYS["user_wallets"].format(user_id="*")
                keys = await self.redis.scan_keys(pattern)

                for key_str in keys:
                    match = _USER_WALLETS_KEY_PATTERN.search(key_str)
                    if not match:
                        continue
                    user_id = int(match.group(1))

                    wallets_data = await self.redis.hgetall(key_str)
                    for wallet_key, wallet_data in wallets_data.items():
                        try:
                            if isinstance(wallet_data, str):
                                wallet_data = json.loads(wallet_data)
                            wallet = TrackedWallet.from_dict(wallet_data)

                            if wallet.exchange not in ("polymarket", "all"):
                                continue

                            await self._check_wallet_activity(wallet, user_id)
                        except Exception as e:
                            logger.error(f"Error processing wallet {wallet_key}: {e}")

                await asyncio.sleep(self.poll_interval)

            except Exception as e:
                logger.error(f"Error in wallet polling loop: {e}")
                await asyncio.sleep(self.poll_interval)

    async def _check_wallet_activity(self, wallet: TrackedWallet, user_id: int) -> None:
        """Check a wallet for new trading activity.

        Args:
            wallet: Tracked wallet to check
            user_id: Telegram user ID who is tracking this wallet
        """
        last_check_key = f"pm:last_check:{wallet.address}"
        last_check = await self.redis.get(last_check_key)
        last_timestamp = int(last_check) if last_check else None

        trades = await self.tracker.detect_whale_trades(
            address=wallet.address,
            since_timestamp=last_timestamp,
        )

        for trade in trades:
            await self._send_trade_alert(wallet, trade, user_id)

        now_ts = str(int(datetime.now(tz=timezone.utc).timestamp()))
        await self.redis.set(last_check_key, now_ts)

    async def _send_trade_alert(
        self, wallet: TrackedWallet, trade: Activity, user_id: int
    ) -> None:
        """Send alert for a tracked wallet trade.

        Args:
            wallet: The tracked wallet
            trade: The trade activity
            user_id: Telegram user ID to send to
        """
        side_str = trade.side.value if trade.side else "TRADE"
        side_emoji = "🟢" if side_str == "BUY" else "🔴"
        label_str = f" ({escape_html(wallet.label)})" if wallet.label else ""
        market_title = escape_html(trade.title or "Unknown Market")

        message = (
            f"{side_emoji} <b>Tracked Wallet Trade</b>\n\n"
            f"<b>Wallet:</b> <code>{shorten_address(wallet.address)}</code>{label_str}\n"
            f"<b>Market:</b> {market_title}\n"
            f"<b>Position:</b> {escape_html(trade.outcome or '')}\n"
            f"<b>Side:</b> {side_str}\n"
            f"<b>Size:</b> ${trade.usdc_size:,.2f}\n"
        )

        if trade.price:
            message += f"<b>Price:</b> {trade.price * 100:.1f}%\n"

        if trade.event_slug:
            message += (
                f"\n<a href=\"https://polymarket.com/event/{trade.event_slug}\">View Market</a>"
            )
        if trade.transaction_hash:
            message += (
                f" | <a href=\"https://polygonscan.com/tx/{trade.transaction_hash}\">View Tx</a>"
            )

        await self.notifications.send_message(
            chat_id=user_id,
            text=message,
            topic_name="polymarket",
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
                    if await self.redis.exists(processed_key):
                        continue

                    await self.redis.set(processed_key, "1", ex=86400)

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
        subscribers_key = "pm:whale_subscribers"
        subscribers = await self.redis.smembers(subscribers_key)

        if not subscribers:
            return

        alert = WhaleAlert(
            exchange="polymarket",
            wallet=trade.proxy_wallet,
            alert_type="whale_trade",
            trade_value=trade.usdc_size,
            side=trade.side.value if trade.side else None,
            market_title=trade.title,
            price=trade.price,
            timestamp=trade.timestamp if isinstance(trade.timestamp, (int, float)) else time.time(),
        )

        for chat_id in subscribers:
            try:
                await self.notifications.send_whale_alert(
                    chat_id=int(chat_id),
                    alert=alert,
                )
            except Exception as e:
                logger.error(f"Error sending whale alert to {chat_id}: {e}")

    async def subscribe_to_whales(self, chat_id: int) -> bool:
        """Subscribe a chat to whale alerts.

        Args:
            chat_id: Telegram chat ID

        Returns:
            True if newly subscribed, False if already subscribed
        """
        key = "pm:whale_subscribers"
        result = await self.redis.sadd(key, str(chat_id))
        return result > 0

    async def unsubscribe_from_whales(self, chat_id: int) -> bool:
        """Unsubscribe a chat from whale alerts.

        Args:
            chat_id: Telegram chat ID

        Returns:
            True if unsubscribed, False if wasn't subscribed
        """
        key = "pm:whale_subscribers"
        result = await self.redis.srem(key, str(chat_id))
        return result > 0

    # --- Query methods for bot commands ---

    async def get_positions(self, address: str) -> list[Position]:
        """Get positions for a wallet address."""
        return await self.tracker.get_wallet_positions(address)

    async def get_pnl(self, address: str, period: str = "all"):
        """Get PnL summary for a wallet."""
        return await self.tracker.get_wallet_pnl(address, period)

    async def get_position_summary(self, address: str) -> dict:
        """Get position summary for a wallet."""
        return await self.tracker.get_position_summary(address)

    async def get_recent_trades(self, address: str, hours: int = 24):
        """Get recent trades for a wallet."""
        return await self.client.get_recent_trades(address, hours)
