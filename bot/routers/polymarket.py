"""Polymarket commands router."""

import logging
from typing import Optional, List, Dict, Any

from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command, CommandObject

from shared.utils import shorten_address, is_valid_ethereum_address
from indexers.polymarket import PolymarketIndexer, Position
from bot.utils.filters import parse_filter_args, format_filter_summary
from bot.storage.redis_client import RedisClient

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


def format_pnl(usd_value: float, percent_value: float) -> str:
    """Format PNL as USD with percentage in parentheses."""
    usd_str = f"+${usd_value:,.2f}" if usd_value >= 0 else f"-${abs(usd_value):,.2f}"
    pct_str = f"+{percent_value:.1f}%" if percent_value >= 0 else f"{percent_value:.1f}%"
    return f"{usd_str} ({pct_str})"


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
            pnl_str = format_pnl(pos.cash_pnl, pos.percent_pnl)
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
                f"   {trade.outcome} | ${trade.usdc_size:,.0f} @ {trade.price*100:.0f}%\n"
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
    """Show recent whale trades on Polymarket with optional filters.

    Usage: /pm_whales [filter]
    Filter examples:
      /pm_whales 50k          - trades above $50k
      /pm_whales >100k        - trades above $100k
      /pm_whales <50k         - trades below $50k
      /pm_whales 10k-50k      - trades between $10k-$50k
      /pm_whales bitcoin      - filter by market name
      /pm_whales trump >50k   - combine text and size filter
    """
    min_size = 10000.0
    max_size = None
    text_filter = None

    if command.args:
        text_filter, parsed_min, parsed_max = parse_filter_args(command.args)
        if parsed_min is not None:
            min_size = parsed_min
        if parsed_max is not None:
            max_size = parsed_max

    if not polymarket_indexer:
        await message.reply("Polymarket indexer is not available.")
        return

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        # Fetch more trades if we need to filter
        fetch_limit = 50 if text_filter or max_size else 15
        trades = await polymarket_indexer.tracker.get_recent_whale_trades(
            min_size=min_size,
            limit=fetch_limit,
        )

        # Apply additional filters
        if max_size is not None:
            trades = [t for t in trades if t.usdc_size <= max_size]

        if text_filter:
            text_lower = text_filter.lower()
            trades = [t for t in trades if text_lower in t.title.lower() or text_lower in t.outcome.lower()]

        if not trades:
            filter_desc = format_filter_summary(text_filter, min_size, max_size)
            await message.reply(
                f"No whale trades found matching: {filter_desc}",
                parse_mode="HTML",
            )
            return

        # Build header
        filter_desc = format_filter_summary(text_filter, min_size, max_size)
        lines = [
            f"<b>🐋 Polymarket Whale Trades</b>",
            f"<i>Filter: {filter_desc}</i>\n",
        ]

        for trade in trades[:15]:
            side_emoji = "🟢" if trade.side.value == "BUY" else "🔴"
            time_str = trade.datetime.strftime("%m/%d %H:%M")

            # Calculate potential PNL (profit if position wins)
            if trade.side.value == "BUY":
                pnl = trade.usdc_size * (1 - trade.price) / trade.price if trade.price > 0 else 0
                pnl_pct = (1 - trade.price) / trade.price * 100 if trade.price > 0 else 0
            else:
                pnl = trade.usdc_size * trade.price / (1 - trade.price) if trade.price < 1 else 0
                pnl_pct = trade.price / (1 - trade.price) * 100 if trade.price < 1 else 0

            # Format: $1,234 (+150%)
            pnl_str = f"${pnl:,.0f} (+{pnl_pct:.0f}%)"

            lines.append(
                f"{side_emoji} <b>${trade.usdc_size:,.0f}</b> | PNL: {pnl_str}\n"
                f"   {trade.title[:45]}{'...' if len(trade.title) > 45 else ''}\n"
                f"   {trade.outcome} @ {trade.price*100:.0f}% | {time_str}"
            )

        if len(trades) > 15:
            lines.append(f"\n<i>... and {len(trades) - 15} more</i>")

        await message.reply("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error getting whale trades: {e}")
        await message.reply(f"Error fetching whale trades: {str(e)}")


@router.message(Command("pm_indexed"))
async def cmd_pm_indexed(
    message: Message,
    command: CommandObject,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Show indexed Polymarket trades from Redis with filters.

    Usage: /pm_indexed [filter]
    Filter examples:
      /pm_indexed             - all recent trades
      /pm_indexed >40k        - trades above $40k
      /pm_indexed trump       - filter by market name
      /pm_indexed bitcoin 10k - combine filters
    """
    if not redis_client:
        await message.reply("Redis is not available. Indexer may not be running.")
        return

    # Parse filters
    text_filter, min_size, max_size = None, None, None
    if command.args:
        text_filter, min_size, max_size = parse_filter_args(command.args)

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        # Get recent trades from Redis
        tx_hashes = await redis_client.zrange("pm:trades", 0, 99, desc=True)

        if not tx_hashes:
            await message.reply("No indexed trades found. Indexer may not be running.")
            return

        trades = []
        for tx_hash in tx_hashes:
            trade_data = await redis_client.redis.hgetall(f"pm:trade:{tx_hash}")
            if trade_data:
                # Apply filters
                usdc_size = float(trade_data.get("usdc_size", 0))
                market_title = trade_data.get("event_title", "")
                outcome = trade_data.get("outcome", "")

                # Size filter
                if min_size is not None and usdc_size < min_size:
                    continue
                if max_size is not None and usdc_size > max_size:
                    continue

                # Text filter
                if text_filter:
                    text_lower = text_filter.lower()
                    if text_lower not in market_title.lower() and text_lower not in outcome.lower():
                        continue

                trades.append(trade_data)
                if len(trades) >= 15:
                    break

        if not trades:
            filter_desc = format_filter_summary(text_filter, min_size, max_size)
            await message.reply(
                f"No indexed trades found matching: {filter_desc}",
                parse_mode="HTML",
            )
            return

        filter_desc = format_filter_summary(text_filter, min_size, max_size)
        lines = [
            f"<b>📊 Indexed Polymarket Trades</b>",
            f"<i>Filter: {filter_desc}</i>\n",
        ]

        for trade in trades:
            usdc_size = float(trade.get("usdc_size", 0))
            price = float(trade.get("price", 0))
            side = trade.get("side", "")
            title = trade.get("event_title", "Unknown")
            outcome = trade.get("outcome", "")
            time_str = trade.get("timestamp_str", "")[-8:]  # Just time part

            side_emoji = "🟢" if side == "BUY" else "🔴"

            # Calculate potential PNL (profit if position wins)
            if side == "BUY":
                pnl = usdc_size * (1 - price) / price if price > 0 else 0
                pnl_pct = (1 - price) / price * 100 if price > 0 else 0
            else:
                pnl = usdc_size * price / (1 - price) if price < 1 else 0
                pnl_pct = price / (1 - price) * 100 if price < 1 else 0

            # Format: $1,234 (+150%)
            pnl_str = f"${pnl:,.0f} (+{pnl_pct:.0f}%)"

            lines.append(
                f"{side_emoji} <b>${usdc_size:,.0f}</b> | PNL: {pnl_str}\n"
                f"   {title[:45]}{'...' if len(title) > 45 else ''}\n"
                f"   {outcome} @ {price*100:.0f}% | {time_str}"
            )

        await message.reply("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error getting indexed trades: {e}")
        await message.reply(f"Error fetching indexed trades: {str(e)}")


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
