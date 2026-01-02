"""General commands router (help, start, etc.)."""

import logging
from typing import Optional

from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command, CommandStart

from bot.storage.redis_client import RedisClient

logger = logging.getLogger(__name__)

router = Router(name="general")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Handle /start command."""
    user_name = message.from_user.first_name or "there"

    await message.reply(
        f"<b>👋 Welcome to PrintDAO, {user_name}!</b>\n\n"
        "I track whale activity across prediction markets and perpetual futures.\n\n"
        "<b>🔷 Supported Exchanges:</b>\n"
        "• Hyperliquid (perpetual futures)\n"
        "• Polymarket (prediction markets)\n\n"
        "<b>🚀 Quick Start:</b>\n"
        "1. Use /track &lt;address&gt; to track a wallet\n"
        "2. Use /list to see your tracked wallets\n"
        "3. Get alerts when tracked wallets trade!\n\n"
        "Use /help to see all available commands.",
        parse_mode="HTML",
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Handle /help command."""
    help_text = """<b>📖 PrintDAO Commands</b>

<b>Wallet Tracking:</b>
/track &lt;address&gt; [label] - Track a wallet
/untrack &lt;address|label&gt; - Stop tracking
/list - List your tracked wallets

<b>Wallet Groups:</b>
/creategroup &lt;name&gt; [desc] - Create a group
/groups - List your groups
/groupinfo &lt;name&gt; - Show group details
/addtogroup &lt;group&gt; &lt;wallet&gt; - Add to group
/deletegroup &lt;name&gt; - Delete a group

<b>Polymarket:</b>
/pm_positions &lt;address&gt; - Show positions
/pm_pnl &lt;address&gt; [period] - Show PnL
/pm_trades &lt;address&gt; - Recent trades
/pm_whales [min_size] - Whale trades
/pm_subscribe - Get whale alerts
/pm_unsubscribe - Stop whale alerts

<b>Hyperliquid:</b>
/hl_positions &lt;address&gt; - Show positions
/hl_pnl &lt;address&gt; [period] - Show PnL
/hl_sentiment &lt;coin&gt; - Market sentiment

<b>Other:</b>
/help - Show this message
/status - Bot status

<i>Tip: Add --exchange=polymarket to filter by exchange</i>"""

    await message.reply(help_text, parse_mode="HTML")


@router.message(Command("status"))
async def cmd_status(message: Message, redis_client: Optional[RedisClient] = None) -> None:
    """Handle /status command."""
    status_text = "<b>🤖 PrintDAO Status</b>\n\n"
    status_text += "✅ Bot is running\n"

    if redis_client:
        try:
            await redis_client.redis.ping()
            status_text += "✅ Redis connected\n"
        except Exception:
            status_text += "❌ Redis disconnected\n"
    else:
        status_text += "⚠️ Redis not configured\n"

    status_text += "\n<b>Supported Exchanges:</b>\n"
    status_text += "• Hyperliquid\n"
    status_text += "• Polymarket\n"

    await message.reply(status_text, parse_mode="HTML")