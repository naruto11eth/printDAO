"""Notification service for sending alerts to Telegram."""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.storage.models import WhaleAlert, CopyTradeSignal, NotificationPreference
from shared.utils import shorten_address
from shared.formatting import format_usd, format_percent

logger = logging.getLogger(__name__)


class NotificationService:
    """Handles sending notifications to Telegram chats/topics."""

    def __init__(self, bot: Bot, redis_client=None):
        """Initialize notification service.

        Args:
            bot: aiogram Bot instance
            redis_client: Optional RedisClient for checking user preferences
        """
        self.bot = bot
        self.redis = redis_client

    async def should_notify(self, user_id: int) -> bool:
        """Check if a user should receive notifications based on their preferences.

        Returns True if notifications should be sent, False if muted or disabled.
        """
        if not self.redis:
            return True

        prefs_data = await self.redis.get(f"user:{user_id}:preferences")
        if not prefs_data:
            return True

        if isinstance(prefs_data, dict):
            pref = prefs_data.get("notification_preference", "all")
            if pref == NotificationPreference.NONE.value:
                return False

            muted_until = prefs_data.get("muted_until")
            if muted_until and float(muted_until) > time.time():
                return False

        return True

    async def register_topic(self, chat_id: int, topic_name: str, topic_id: int) -> None:
        """Register a forum topic for a chat. Persisted to Redis."""
        if self.redis:
            key = f"chat_topics:{chat_id}"
            await self.redis.hset(key, topic_name, str(topic_id))
        logger.info(f"Registered topic {topic_name}={topic_id} for chat {chat_id}")

    async def get_topic_id(self, chat_id: int, topic_name: str) -> Optional[int]:
        """Get topic ID for a chat from Redis."""
        if not self.redis:
            return None
        key = f"chat_topics:{chat_id}"
        value = await self.redis.hget(key, topic_name)
        if value is not None:
            return int(value)
        return None

    async def send_message(
        self,
        chat_id: int,
        text: str,
        topic_name: Optional[str] = None,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        parse_mode: str = "HTML",
    ) -> bool:
        """Send a message to a chat, optionally to a specific topic.

        Args:
            chat_id: Telegram chat ID
            text: Message text
            topic_name: Optional topic to send to (if forum topics enabled)
            reply_markup: Optional inline keyboard
            parse_mode: Parse mode (HTML, Markdown, etc.)

        Returns:
            True if sent successfully, False otherwise
        """
        try:
            message_thread_id = None
            if topic_name:
                message_thread_id = await self.get_topic_id(chat_id, topic_name)

            await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                message_thread_id=message_thread_id,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send message to chat {chat_id}: {e}")
            return False

    async def send_whale_alert(
        self,
        chat_id: int,
        alert: WhaleAlert,
        include_copy_buttons: bool = True,
    ) -> bool:
        """Send a whale alert notification.

        Args:
            chat_id: Telegram chat ID
            alert: WhaleAlert object
            include_copy_buttons: Whether to include copy address buttons

        Returns:
            True if sent successfully
        """
        if not await self.should_notify(chat_id):
            return False

        topic_name = alert.exchange

        # Format the alert message
        text = self._format_whale_alert(alert)

        # Create inline keyboard with copy buttons
        reply_markup = None
        if include_copy_buttons:
            buttons = [
                [
                    InlineKeyboardButton(
                        text="Copy Address",
                        callback_data=f"copy:{alert.wallet}",
                    )
                ]
            ]
            # Add track button
            buttons.append([
                InlineKeyboardButton(
                    text="Track Wallet",
                    callback_data=f"track:{alert.wallet}:{alert.exchange}",
                )
            ])
            reply_markup = InlineKeyboardMarkup(inline_keyboard=buttons)

        return await self.send_message(
            chat_id=chat_id,
            text=text,
            topic_name=topic_name,
            reply_markup=reply_markup,
        )

    async def send_copy_signal(
        self,
        chat_id: int,
        signal: CopyTradeSignal,
    ) -> bool:
        """Send a copy trading signal notification.

        Args:
            chat_id: Telegram chat ID
            signal: CopyTradeSignal object

        Returns:
            True if sent successfully
        """
        text = self._format_copy_signal(signal)

        buttons = [
            [
                InlineKeyboardButton(
                    text="View Position",
                    callback_data=f"position:{signal.source_wallet}:{signal.exchange}",
                ),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(inline_keyboard=buttons)

        return await self.send_message(
            chat_id=chat_id,
            text=text,
            topic_name="copy_trading",
            reply_markup=reply_markup,
        )

    async def broadcast_to_subscribers(
        self,
        subscriber_ids: List[int],
        text: str,
        topic_name: Optional[str] = None,
    ) -> int:
        """Broadcast a message to multiple subscribers.

        Args:
            subscriber_ids: List of chat IDs to send to
            text: Message text
            topic_name: Optional topic name

        Returns:
            Number of successful sends
        """
        semaphore = asyncio.Semaphore(20)

        async def _send_one(chat_id: int) -> bool:
            if not await self.should_notify(chat_id):
                return False
            async with semaphore:
                return await self.send_message(chat_id, text, topic_name)

        results = await asyncio.gather(
            *[_send_one(cid) for cid in subscriber_ids],
            return_exceptions=True,
        )
        return sum(1 for r in results if r is True)

    def _format_whale_alert(self, alert: WhaleAlert) -> str:
        """Format a whale alert as HTML message."""
        short_addr = shorten_address(alert.wallet)
        value_str = format_usd(alert.trade_value)

        if alert.alert_type == "new_whale_discovered":
            emoji = "🐋"
            title = "NEW WHALE DISCOVERED"
        else:
            emoji = "🚨"
            title = "WHALE ALERT"

        lines = [
            f"<b>{emoji} {title}</b>",
            "",
            f"Wallet: <code>{short_addr}</code>",
            f"Exchange: {alert.exchange.upper()}",
        ]

        if alert.market_title:
            lines.append(f"Market: {alert.market_title}")

        if alert.side:
            lines.append(f"Side: <b>{alert.side}</b>")

        if alert.asset:
            lines.append(f"Asset: {alert.asset}")

        lines.append(f"Value: <b>{value_str}</b>")

        if alert.price:
            lines.append(f"Price: ${alert.price:,.4f}")

        if alert.size:
            lines.append(f"Size: {alert.size:,.4f}")

        return "\n".join(lines)

    def _format_copy_signal(self, signal: CopyTradeSignal) -> str:
        """Format a copy trading signal as HTML message."""
        wallet_display = signal.source_label or shorten_address(signal.source_wallet)
        notional_str = format_usd(signal.notional_usd)

        lines = [
            "<b>🔔 COPY TRADE SIGNAL</b>",
            "",
            f"Trader: <code>{wallet_display}</code>",
            f"Exchange: {signal.exchange.upper()}",
            "",
        ]

        if signal.market_title:
            lines.append(f"📊 Market: {signal.market_title}")

        lines.extend([
            f"Action: <b>{signal.action}</b>",
            f"Size: {signal.amount:,.4f} @ ${signal.price:,.4f}",
            f"Notional: {notional_str}",
        ])

        if signal.leverage:
            lines.append(f"Leverage: {signal.leverage}x")

        if signal.outcome:
            lines.append(f"Outcome: {signal.outcome}")

        lines.extend([
            "",
            "<i>⚠️ Not financial advice. DYOR.</i>",
        ])

        return "\n".join(lines)
