"""Polymarket commands router."""

import logging
from typing import Optional

from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command, CommandObject

from shared.utils import shorten_address, is_valid_ethereum_address
from indexers.polymarket import PolymarketIndexer, Position

logger = logging.getLogger(__name__)

router = Router(name="polymarket")


def format_usd(value: float) -> str:
    """Format USD value with sign."""
    if value >= 0:
        return f"+${value:,.2f}"
    return f"-${abs(value):,.2f}"


def format_percent(value: float) -> str:
    """Format percentage with sign."""
    if value >= 0:
        return f"+{value:.1f}%"
    return f"{value:.1f}%"


@router.message(Command("pm_positions"))
async def cmd_pm_positions(
    message: Message,
    command: CommandObject,
    polymarket_indexer: Optional[PolymarketIndexer] = None,
) -> None:
    """Show Polymarket positions for an address.

    Usage: /pm_positions <address>
    """
    if not command.args:
        await message.reply(
            "<b>Usage:</b> /pm_positions &lt;address&gt;\n\n"
            "<b>Example:</b>\n"
            "<code>/pm_positions 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb</code>",
            parse_mode="HTML",
        )
        return

    address = command.args.strip()

    if not is_valid_ethereum_address(address):
        await message.reply("Invalid Ethereum address. Please provide a valid 0x address.")
        return

    if not polymarket_indexer:
        await message.reply("Polymarket indexer is not available.")
        return

    # Show typing indicator
    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        summary = await polymarket_indexer.get_position_summary(address)
        positions = summary["positions"]

        if not positions:
            await message.reply(
                f"No positions found for <code>{shorten_address(address)}</code>",
                parse_mode="HTML",
            )
            return

        # Build response
        lines = [
            f"<b>Polymarket Positions</b>",
            f"<code>{shorten_address(address)}</code>\n",
            f"<b>Portfolio Value:</b> ${summary['total_value']:,.2f}",
            f"<b>Total PnL:</b> {format_usd(summary['total_pnl'])}",
            f"<b>Positions:</b> {summary['position_count']} ({summary['winning_positions']}W / {summary['losing_positions']}L)\n",
            "<b>Top Positions:</b>",
        ]

        # Show top 5 positions
        for i, pos in enumerate(positions[:5], 1):
            pnl_str = format_usd(pos.cash_pnl)
            pnl_color = "🟢" if pos.cash_pnl >= 0 else "🔴"

            lines.append(
                f"{i}. {pnl_color} <b>{pos.title[:40]}{'...' if len(pos.title) > 40 else ''}</b>\n"
                f"   {pos.outcome} @ {pos.avg_price:.1f}% | Now: {pos.cur_price:.1f}%\n"
                f"   Size: ${pos.current_value:,.0f} | PnL: {pnl_str}"
            )

        if len(positions) > 5:
            lines.append(f"\n<i>... and {len(positions) - 5} more positions</i>")

        await message.reply("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error getting positions for {address}: {e}")
        await message.reply(f"Error fetching positions: {str(e)}")


@router.message(Command("pm_pnl"))
async def cmd_pm_pnl(
    message: Message,
    command: CommandObject,
    polymarket_indexer: Optional[PolymarketIndexer] = None,
) -> None:
    """Show PnL for a Polymarket address.

    Usage: /pm_pnl <address> [period]
    Periods: day, week, month, all (default)
    """
    if not command.args:
        await message.reply(
            "<b>Usage:</b> /pm_pnl &lt;address&gt; [period]\n\n"
            "<b>Periods:</b> day, week, month, all (default)\n\n"
            "<b>Example:</b>\n"
            "<code>/pm_pnl 0x742d... week</code>",
            parse_mode="HTML",
        )
        return

    parts = command.args.strip().split()
    address = parts[0]
    period = parts[1].lower() if len(parts) > 1 else "all"

    if period not in ("day", "week", "month", "all"):
        await message.reply("Invalid period. Use: day, week, month, or all")
        return

    if not is_valid_ethereum_address(address):
        await message.reply("Invalid Ethereum address.")
        return

    if not polymarket_indexer:
        await message.reply("Polymarket indexer is not available.")
        return

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        pnl = await polymarket_indexer.get_pnl(address, period)

        period_labels = {
            "day": "24 Hours",
            "week": "7 Days",
            "month": "30 Days",
            "all": "All Time",
        }

        pnl_emoji = "🟢" if pnl.is_profitable else "🔴"

        lines = [
            f"<b>Polymarket PnL - {period_labels[period]}</b>",
            f"<code>{shorten_address(address)}</code>\n",
            f"{pnl_emoji} <b>Total PnL:</b> {format_usd(pnl.total_pnl)}",
            f"<b>Realized:</b> {format_usd(pnl.realized_pnl)}",
            f"<b>Unrealized:</b> {format_usd(pnl.unrealized_pnl)}\n",
            f"<b>Win Rate:</b> {pnl.win_rate:.1f}%",
            f"<b>Trades:</b> {pnl.total_trades}",
            f"<b>W/L:</b> {pnl.winning_trades}W / {pnl.losing_trades}L",
        ]

        await message.reply("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error getting PnL for {address}: {e}")
        await message.reply(f"Error fetching PnL: {str(e)}")


@router.message(Command("pm_trades"))
async def cmd_pm_trades(
    message: Message,
    command: CommandObject,
    polymarket_indexer: Optional[PolymarketIndexer] = None,
) -> None:
    """Show recent trades for a Polymarket address.

    Usage: /pm_trades <address> [hours]
    """
    if not command.args:
        await message.reply(
            "<b>Usage:</b> /pm_trades &lt;address&gt; [hours]\n\n"
            "<b>Example:</b>\n"
            "<code>/pm_trades 0x742d... 24</code>",
            parse_mode="HTML",
        )
        return

    parts = command.args.strip().split()
    address = parts[0]
    hours = int(parts[1]) if len(parts) > 1 else 24

    if not is_valid_ethereum_address(address):
        await message.reply("Invalid Ethereum address.")
        return

    if not polymarket_indexer:
        await message.reply("Polymarket indexer is not available.")
        return

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        trades = await polymarket_indexer.get_recent_trades(address, hours)

        if not trades:
            await message.reply(
                f"No trades in the last {hours} hours for <code>{shorten_address(address)}</code>",
                parse_mode="HTML",
            )
            return

        lines = [
            f"<b>Recent Trades ({hours}h)</b>",
            f"<code>{shorten_address(address)}</code>\n",
        ]

        for trade in trades[:10]:
            side_emoji = "🟢" if trade.side and trade.side.value == "BUY" else "🔴"
            time_str = trade.datetime.strftime("%m/%d %H:%M")

            lines.append(
                f"{side_emoji} <b>{trade.title[:35]}{'...' if len(trade.title) > 35 else ''}</b>\n"
                f"   {trade.outcome} | ${trade.usdc_size:,.0f} @ {trade.price:.1f}%\n"
                f"   {time_str}"
            )

        if len(trades) > 10:
            lines.append(f"\n<i>... and {len(trades) - 10} more trades</i>")

        await message.reply("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error getting trades for {address}: {e}")
        await message.reply(f"Error fetching trades: {str(e)}")


@router.message(Command("pm_whales"))
async def cmd_pm_whales(
    message: Message,
    command: CommandObject,
    polymarket_indexer: Optional[PolymarketIndexer] = None,
) -> None:
    """Show recent whale trades on Polymarket.

    Usage: /pm_whales [min_size]
    Default min_size: $10,000
    """
    min_size = 10000.0

    if command.args:
        try:
            min_size = float(command.args.strip().replace("$", "").replace(",", ""))
        except ValueError:
            await message.reply("Invalid amount. Use a number like 10000 or $10,000")
            return

    if not polymarket_indexer:
        await message.reply("Polymarket indexer is not available.")
        return

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        trades = await polymarket_indexer.tracker.get_recent_whale_trades(
            min_size=min_size,
            limit=10,
        )

        if not trades:
            await message.reply(
                f"No whale trades found above ${min_size:,.0f}",
                parse_mode="HTML",
            )
            return

        lines = [
            f"<b>🐋 Polymarket Whale Trades</b>",
            f"<i>Trades above ${min_size:,.0f}</i>\n",
        ]

        for trade in trades:
            side_emoji = "🟢" if trade.side.value == "BUY" else "🔴"
            trader = trade.pseudonym or trade.name or shorten_address(trade.proxy_wallet)
            time_str = trade.datetime.strftime("%m/%d %H:%M")

            lines.append(
                f"{side_emoji} <b>${trade.usdc_size:,.0f}</b> by {trader}\n"
                f"   {trade.title[:40]}{'...' if len(trade.title) > 40 else ''}\n"
                f"   {trade.outcome} @ {trade.price:.1f}% | {time_str}"
            )

        await message.reply("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error getting whale trades: {e}")
        await message.reply(f"Error fetching whale trades: {str(e)}")


@router.message(Command("pm_subscribe"))
async def cmd_pm_subscribe(
    message: Message,
    polymarket_indexer: Optional[PolymarketIndexer] = None,
) -> None:
    """Subscribe to Polymarket whale alerts."""
    if not polymarket_indexer:
        await message.reply("Polymarket indexer is not available.")
        return

    try:
        is_new = await polymarket_indexer.subscribe_to_whales(message.chat.id)

        if is_new:
            await message.reply(
                "🐋 <b>Subscribed to Polymarket whale alerts!</b>\n\n"
                "You'll receive alerts for trades above $10,000.\n"
                "Use /pm_unsubscribe to stop.",
                parse_mode="HTML",
            )
        else:
            await message.reply("You're already subscribed to whale alerts.")

    except Exception as e:
        logger.error(f"Error subscribing to whales: {e}")
        await message.reply(f"Error: {str(e)}")


@router.message(Command("pm_unsubscribe"))
async def cmd_pm_unsubscribe(
    message: Message,
    polymarket_indexer: Optional[PolymarketIndexer] = None,
) -> None:
    """Unsubscribe from Polymarket whale alerts."""
    if not polymarket_indexer:
        await message.reply("Polymarket indexer is not available.")
        return

    try:
        was_subscribed = await polymarket_indexer.unsubscribe_from_whales(message.chat.id)

        if was_subscribed:
            await message.reply(
                "✅ Unsubscribed from Polymarket whale alerts.\n"
                "Use /pm_subscribe to re-subscribe.",
                parse_mode="HTML",
            )
        else:
            await message.reply("You weren't subscribed to whale alerts.")

    except Exception as e:
        logger.error(f"Error unsubscribing from whales: {e}")
        await message.reply(f"Error: {str(e)}")
