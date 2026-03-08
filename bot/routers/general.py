"""General commands router (help, start, etc.)."""

import logging
from typing import Optional

from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command, CommandStart, CommandObject

from bot.storage.redis_client import RedisClient
from bot.services.whale_detector import ConfigurableWhaleDetector
from bot.utils.auth import admin_required
from shared.formatting import format_usd

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
/pm_whales [filter] - Whale trades
/pm_indexed [filter] - Indexed trades
/pm_subscribe - Get whale alerts
/pm_unsubscribe - Stop whale alerts

<b>Hyperliquid:</b>
/hl_positions &lt;address&gt; - Positions
/hl_pnl &lt;address&gt; - PnL summary
/hl_fills &lt;address&gt; - Recent fills
/hl_analytics &lt;address&gt; - Trader analytics
/hl_trades &lt;coin&gt; [filter] - Recent trades
/hl_sentiment &lt;coin&gt; - Sentiment
/hl_market &lt;coin&gt; - Market info
/hl_whales [filter] - Whale trades
/hl_indexed [filter] - Indexed trades
/hl_coins - List coins

<b>Filter Examples:</b>
<code>&gt;40k</code> or <code>40k+</code> - Above $40k
<code>&lt;100k</code> - Below $100k
<code>10k-50k</code> - Between $10k-$50k
<code>btc</code> or <code>trump</code> - Text search
<code>btc &gt;500k</code> - Combine filters

<b>Whale Config:</b>
/whale_config - Show config
/set_whale_threshold - Set threshold
/toggle_new_whale - Toggle detection
/new_whales - Show new whales

<b>Other:</b>
/help - Show this message
/status - Bot status"""

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


@router.message(Command("whale_config"))
async def cmd_whale_config(
    message: Message,
    whale_detector: Optional[ConfigurableWhaleDetector] = None,
) -> None:
    """Show current whale detection configuration."""
    if not whale_detector:
        await message.reply("Whale detector is not available.")
        return

    config = whale_detector.config

    lines = [
        "<b>🐋 Whale Detection Config</b>\n",
        "<b>Thresholds:</b>",
        f"  Hyperliquid: {format_usd(config.hyperliquid_threshold)}",
        f"  Polymarket: {format_usd(config.polymarket_threshold)}\n",
        "<b>New Wallet Detection:</b>",
        f"  Enabled: {'✅' if config.new_wallet_detection_enabled else '❌'}",
        f"  First Trade Threshold: {format_usd(config.new_wallet_first_trade_threshold)}",
        f"  Age Window: {config.new_wallet_age_hours}h\n",
        "<b>Alerts:</b>",
        f"  Cooldown: {config.alert_cooldown_seconds}s\n",
        "<i>Use /set_whale_threshold to adjust</i>",
    ]

    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("set_whale_threshold"))
@admin_required
async def cmd_set_whale_threshold(
    message: Message,
    command: CommandObject,
    whale_detector: Optional[ConfigurableWhaleDetector] = None,
) -> None:
    """Set whale detection threshold.

    Usage: /set_whale_threshold <exchange> <amount>
    """
    if not command.args:
        await message.reply(
            "<b>Usage:</b> /set_whale_threshold &lt;exchange&gt; &lt;amount&gt;\n\n"
            "<b>Examples:</b>\n"
            "<code>/set_whale_threshold hyperliquid 5M</code>\n"
            "<code>/set_whale_threshold polymarket 25000</code>\n"
            "<code>/set_whale_threshold newwallet 100000</code>",
            parse_mode="HTML",
        )
        return

    if not whale_detector:
        await message.reply("Whale detector is not available.")
        return

    parts = command.args.strip().lower().split()
    if len(parts) < 2:
        await message.reply("Please provide both exchange and amount.")
        return

    exchange = parts[0]
    amount_str = parts[1].upper().replace("$", "").replace(",", "")

    # Parse amount (handle M/K suffixes)
    try:
        if amount_str.endswith("M"):
            amount = float(amount_str[:-1]) * 1_000_000
        elif amount_str.endswith("K"):
            amount = float(amount_str[:-1]) * 1_000
        else:
            amount = float(amount_str)
    except ValueError:
        await message.reply("Invalid amount format.")
        return

    # Update config
    if exchange == "hyperliquid" or exchange == "hl":
        await whale_detector.update_config(hyperliquid_threshold=amount)
        await message.reply(
            f"✅ Hyperliquid whale threshold set to {format_usd(amount)}",
            parse_mode="HTML",
        )
    elif exchange == "polymarket" or exchange == "pm":
        await whale_detector.update_config(polymarket_threshold=amount)
        await message.reply(
            f"✅ Polymarket whale threshold set to {format_usd(amount)}",
            parse_mode="HTML",
        )
    elif exchange == "newwallet" or exchange == "new":
        await whale_detector.update_config(new_wallet_first_trade_threshold=amount)
        await message.reply(
            f"✅ New wallet threshold set to {format_usd(amount)}",
            parse_mode="HTML",
        )
    else:
        await message.reply(
            "Unknown exchange. Use: hyperliquid, polymarket, or newwallet"
        )


@router.message(Command("toggle_new_whale"))
@admin_required
async def cmd_toggle_new_whale(
    message: Message,
    whale_detector: Optional[ConfigurableWhaleDetector] = None,
) -> None:
    """Toggle new wallet detection on/off."""
    if not whale_detector:
        await message.reply("Whale detector is not available.")
        return

    current = whale_detector.config.new_wallet_detection_enabled
    await whale_detector.update_config(new_wallet_detection_enabled=not current)

    status = "enabled" if not current else "disabled"
    emoji = "✅" if not current else "❌"

    await message.reply(
        f"{emoji} New wallet detection is now <b>{status}</b>",
        parse_mode="HTML",
    )


@router.message(Command("new_whales"))
async def cmd_new_whales(
    message: Message,
    command: CommandObject,
    whale_detector: Optional[ConfigurableWhaleDetector] = None,
) -> None:
    """Show recently discovered whales.

    Usage: /new_whales [exchange] [hours]
    """
    if not whale_detector:
        await message.reply("Whale detector is not available.")
        return

    parts = command.args.strip().split() if command.args else []
    exchange = parts[0] if parts else "hyperliquid"
    hours = int(parts[1]) if len(parts) > 1 else 24

    await message.bot.send_chat_action(message.chat.id, "typing")

    new_whales = await whale_detector.get_new_whales(exchange, hours=hours)

    if not new_whales:
        await message.reply(
            f"No new whales discovered on {exchange} in the last {hours} hours.",
            parse_mode="HTML",
        )
        return

    lines = [
        f"<b>🐋 New Whales ({exchange})</b>",
        f"<i>Discovered in last {hours} hours</i>\n",
    ]

    for whale in new_whales[:10]:
        addr_short = f"{whale.address[:6]}...{whale.address[-4:]}"
        lines.append(
            f"<code>{addr_short}</code>\n"
            f"  Volume: {format_usd(whale.total_volume)}\n"
            f"  Trades: {whale.total_trades} | Age: {whale.age_hours:.1f}h"
        )

    if len(new_whales) > 10:
        lines.append(f"\n<i>... and {len(new_whales) - 10} more</i>")

    await message.reply("\n".join(lines), parse_mode="HTML")
