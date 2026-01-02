"""Notification service for sending alerts to Telegram."""

import logging
from typing import Any, Dict, List, Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.storage.models import WhaleAlert, CopyTradeSignal
from shared.utils import shorten_address, format_usd, format_percentage

logger = logging.getLogger(__name__)


class NotificationService:
    """Handles sending notifications to Telegram chats/topics."""

    def __init__(self, bot: Bot):
        """Initialize notification service.

        Args:
            bot: aiogram Bot instance
        """
        self.bot = bot
        # Store topic IDs per chat (chat_id -> {topic_name: topic_id})
        self._chat_topics: Dict[int, Dict[str, int]] = {}

    def register_topic(self, chat_id: int, topic_name: str, topic_id: int) -> None:
        """Register a forum topic for a chat.

        Args:
            chat_id: Telegram chat ID
            topic_name: Internal topic name (e.g., "hyperliquid", "polymarket")
            topic_id: Telegram message_thread_id for the topic
        """
        if chat_id not in self._chat_topics:
            self._chat_topics[chat_id] = {}
        self._chat_topics[chat_id][topic_name] = topic_id
        logger.info(f"Registered topic {topic_name}={topic_id} for chat {chat_id}")

    def get_topic_id(self, chat_id: int, topic_name: str) -> Optional[int]:
        """Get topic ID for a chat.

        Args:
            chat_id: Telegram chat ID
            topic_name: Internal topic name

        Returns:
            Topic ID or None if not registered
        """
        return self._chat_topics.get(chat_id, {}).get(topic_name)

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
                message_thread_id = self.get_topic_id(chat_id, topic_name)

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
