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
