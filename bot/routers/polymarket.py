"""Polymarket commands router."""

import logging
from typing import Optional, List, Dict, Any

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from shared.utils import shorten_address, is_valid_ethereum_address
from indexers.polymarket import PolymarketIndexer, Position, Market, Tag, MarketHolder
from indexers.polymarket.client import PolymarketClient
from bot.utils.filters import parse_filter_args, format_filter_summary
from bot.storage.redis_client import RedisClient
from bot.keyboards.polymarket_menu import (
    PMMenuAction,
    PMMarketAction,
    PMCategoryAction,
    PMSearchAction,
    build_pm_main_menu,
    build_pm_top_markets,
    build_pm_market_detail,
    build_pm_positions_keyboard,
    build_pm_categories,
    build_pm_category_markets,
    build_pm_search_results,
    build_pm_news_keyboard,
)

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


# ==================== FSM States ====================


class PMSearchState(StatesGroup):
    """States for search flow."""

    waiting_for_query = State()


# ==================== Helper Functions ====================


def format_market_list(markets: list[Market], start_num: int = 1) -> str:
    """Format markets for display in list view."""
    lines = []
    for i, market in enumerate(markets, start_num):
        # Truncate question
        question = market.question[:50] + "..." if len(market.question) > 50 else market.question

        # Format prices
        yes_pct = int(market.yes_price * 100)
        no_pct = int(market.no_price * 100)

        lines.append(
            f"<b>{i}.</b> {question}\n"
            f"   💰 {market.format_liquidity()} | YES {yes_pct}% | NO {no_pct}%"
        )

    return "\n\n".join(lines)


def format_market_detail(market: Market) -> str:
    """Format single market for detail view."""
    # Format prices
    yes_pct = int(market.yes_price * 100)
    no_pct = int(market.no_price * 100)

    # Format volume
    if market.volume_24h >= 1_000_000:
        vol_str = f"${market.volume_24h / 1_000_000:.2f}M"
    elif market.volume_24h >= 1_000:
        vol_str = f"${market.volume_24h / 1_000:.1f}K"
    else:
        vol_str = f"${market.volume_24h:.0f}"

    # Format open interest
    if market.open_interest >= 1_000_000:
        oi_str = f"${market.open_interest / 1_000_000:.2f}M"
    elif market.open_interest >= 1_000:
        oi_str = f"${market.open_interest / 1_000:.1f}K"
    else:
        oi_str = f"${market.open_interest:.0f}"

    lines = [
        f"📈 <b>{market.question}</b>\n",
        f"💰 <b>Liquidity:</b> {market.format_liquidity()}",
        f"📊 <b>Volume (24h):</b> {vol_str}",
        f"📈 <b>Open Interest:</b> {oi_str}\n",
        f"<b>Current Prices:</b>",
        f"  YES: {yes_pct}¢ (${market.yes_price:.2f})",
        f"  NO: {no_pct}¢ (${market.no_price:.2f})",
    ]

    if market.end_date:
        lines.append(f"\n⏰ <b>Closes:</b> {market.end_date[:10]}")

    if market.tags:
        tags_str = ", ".join(market.tags[:3])
        lines.append(f"🏷️ <b>Tags:</b> {tags_str}")

    return "\n".join(lines)


# ==================== Menu Commands ====================


@router.message(Command("pm"))
@router.message(Command("pm_menu"))
async def cmd_pm_menu(message: Message) -> None:
    """Show main Polymarket menu."""
    text = (
        "📊 <b>Polymarket Menu</b>\n\n"
        "Browse prediction markets with liquidity, search, and real-time news."
    )

    keyboard = build_pm_main_menu()
    await message.reply(text, parse_mode="HTML", reply_markup=keyboard)


@router.message(Command("pm_top"))
async def cmd_pm_top(
    message: Message,
    polymarket_indexer: Optional[PolymarketIndexer] = None,
) -> None:
    """Show top markets by liquidity."""
    if not polymarket_indexer:
        await message.reply("Polymarket is not available.")
        return

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        client = polymarket_indexer.client
        markets = await client.get_markets(active=True, order_by="liquidity", limit=10)

        if not markets:
            await message.reply("No markets found.")
            return

        text = "🔥 <b>Top Markets by Liquidity</b>\n\n" + format_market_list(markets)
        keyboard = build_pm_top_markets(markets, page=0)

        await message.reply(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception as e:
        logger.error(f"Error getting top markets: {e}")
        await message.reply(f"Error: {str(e)}")


@router.message(Command("pm_search"))
async def cmd_pm_search(
    message: Message,
    command: CommandObject,
    polymarket_indexer: Optional[PolymarketIndexer] = None,
    state: FSMContext = None,
) -> None:
    """Search markets by keyword.

    Usage: /pm_search <query>
    """
    if not polymarket_indexer:
        await message.reply("Polymarket is not available.")
        return

    if not command.args:
        await message.reply(
            "<b>Usage:</b> /pm_search &lt;query&gt;\n\n"
            "<b>Examples:</b>\n"
            "<code>/pm_search trump</code>\n"
            "<code>/pm_search bitcoin</code>\n"
            "<code>/pm_search election 2024</code>",
            parse_mode="HTML",
        )
        return

    query = command.args.strip()
    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        client = polymarket_indexer.client
        markets = await client.search_markets(query, limit=10)

        if not markets:
            await message.reply(f"No markets found for: <b>{query}</b>", parse_mode="HTML")
            return

        text = f"🔍 <b>Search Results: {query}</b>\n\n" + format_market_list(markets)
        keyboard = build_pm_search_results(markets, query, page=0)

        await message.reply(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception as e:
        logger.error(f"Error searching markets: {e}")
        await message.reply(f"Error: {str(e)}")


@router.message(Command("pm_market"))
async def cmd_pm_market(
    message: Message,
    command: CommandObject,
    polymarket_indexer: Optional[PolymarketIndexer] = None,
) -> None:
    """Show market details.

    Usage: /pm_market <slug or condition_id>
    """
    if not polymarket_indexer:
        await message.reply("Polymarket is not available.")
        return

    if not command.args:
        await message.reply(
            "<b>Usage:</b> /pm_market &lt;slug&gt;\n\n"
            "<b>Example:</b>\n"
            "<code>/pm_market will-trump-win-2024</code>",
            parse_mode="HTML",
        )
        return

    identifier = command.args.strip()
    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        client = polymarket_indexer.client

        # Try slug first, then condition_id
        market = await client.get_market_by_slug(identifier)
        if not market:
            market = await client.get_market_by_condition_id(identifier)

        if not market:
            await message.reply(f"Market not found: <b>{identifier}</b>", parse_mode="HTML")
            return

        text = format_market_detail(market)
        keyboard = build_pm_market_detail(market)

        await message.reply(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception as e:
        logger.error(f"Error getting market: {e}")
        await message.reply(f"Error: {str(e)}")


# ==================== Callback Handlers ====================


@router.callback_query(PMMenuAction.filter(F.action == "top"))
async def callback_pm_top_markets(
    callback: CallbackQuery,
    callback_data: PMMenuAction,
    polymarket_indexer: Optional[PolymarketIndexer] = None,
) -> None:
    """Show top markets with pagination."""
    await callback.answer()

    if not polymarket_indexer:
        await callback.answer("Polymarket not available", show_alert=True)
        return

    page = callback_data.page
    per_page = 10
    offset = page * per_page

    try:
        client = polymarket_indexer.client
        markets = await client.get_markets(active=True, order_by="liquidity", limit=per_page, offset=offset)

        if not markets:
            await callback.answer("No more markets", show_alert=True)
            return

        start_num = offset + 1
        text = f"🔥 <b>Top Markets by Liquidity</b> (Page {page + 1})\n\n" + format_market_list(markets, start_num)
        keyboard = build_pm_top_markets(markets, page=page)

        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception as e:
        logger.error(f"Error getting top markets: {e}")
        await callback.answer(f"Error: {str(e)[:50]}", show_alert=True)


@router.callback_query(PMMenuAction.filter(F.action == "search"))
async def callback_pm_search_prompt(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Prompt for search query."""
    await callback.answer()

    text = (
        "🔍 <b>Search Markets</b>\n\n"
        "Send me a search query (e.g., 'trump', 'bitcoin', 'election').\n\n"
        "Or use: /pm_search &lt;query&gt;"
    )

    await callback.message.edit_text(text, parse_mode="HTML")
    await state.set_state(PMSearchState.waiting_for_query)


@router.message(PMSearchState.waiting_for_query)
async def process_search_query(
    message: Message,
    state: FSMContext,
    polymarket_indexer: Optional[PolymarketIndexer] = None,
) -> None:
    """Process search query from FSM state."""
    await state.clear()

    if not polymarket_indexer:
        await message.reply("Polymarket is not available.")
        return

    query = message.text.strip()
    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        client = polymarket_indexer.client
        markets = await client.search_markets(query, limit=10)

        if not markets:
            text = f"No markets found for: <b>{query}</b>\n\nTry a different search term."
            keyboard = build_pm_main_menu()
            await message.reply(text, parse_mode="HTML", reply_markup=keyboard)
            return

        text = f"🔍 <b>Search Results: {query}</b>\n\n" + format_market_list(markets)
        keyboard = build_pm_search_results(markets, query, page=0)

        await message.reply(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception as e:
        logger.error(f"Error searching markets: {e}")
        await message.reply(f"Error: {str(e)}")


@router.callback_query(PMMenuAction.filter(F.action == "categories"))
async def callback_pm_categories(
    callback: CallbackQuery,
    polymarket_indexer: Optional[PolymarketIndexer] = None,
) -> None:
    """Show category list."""
    await callback.answer()

    if not polymarket_indexer:
        await callback.answer("Polymarket not available", show_alert=True)
        return

    try:
        client = polymarket_indexer.client
        tags = await client.get_tags()

        if not tags:
            # Use default categories
            tags = [
                Tag(id="1", slug="politics", label="Politics"),
                Tag(id="2", slug="crypto", label="Crypto"),
                Tag(id="3", slug="sports", label="Sports"),
                Tag(id="4", slug="entertainment", label="Entertainment"),
                Tag(id="5", slug="finance", label="Finance"),
            ]

        text = "📁 <b>Browse by Category</b>\n\nSelect a category to see markets:"
        keyboard = build_pm_categories(tags)

        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception as e:
        logger.error(f"Error getting categories: {e}")
        await callback.answer(f"Error: {str(e)[:50]}", show_alert=True)


@router.callback_query(PMMenuAction.filter(F.action == "refresh"))
async def callback_pm_refresh(callback: CallbackQuery) -> None:
    """Refresh main menu."""
    await callback.answer("Refreshed!")

    text = (
        "📊 <b>Polymarket Menu</b>\n\n"
        "Browse prediction markets with liquidity, search, and real-time news."
    )

    keyboard = build_pm_main_menu()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(PMMenuAction.filter(F.action == "back"))
async def callback_pm_back_to_menu(callback: CallbackQuery) -> None:
    """Go back to main menu."""
    await callback.answer()

    text = (
        "📊 <b>Polymarket Menu</b>\n\n"
        "Browse prediction markets with liquidity, search, and real-time news."
    )

    keyboard = build_pm_main_menu()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(PMCategoryAction.filter(F.action == "select"))
async def callback_pm_category_select(
    callback: CallbackQuery,
    callback_data: PMCategoryAction,
    polymarket_indexer: Optional[PolymarketIndexer] = None,
) -> None:
    """Show markets in selected category."""
    await callback.answer()

    if not polymarket_indexer:
        await callback.answer("Polymarket not available", show_alert=True)
        return

    tag_slug = callback_data.tag_slug
    page = callback_data.page
    per_page = 10
    offset = page * per_page

    try:
        client = polymarket_indexer.client
        markets = await client.get_markets(
            active=True,
            order_by="liquidity",
            limit=per_page,
            offset=offset,
            tag_slug=tag_slug,
        )

        if not markets:
            await callback.answer(f"No markets in {tag_slug}", show_alert=True)
            return

        start_num = offset + 1
        text = f"📁 <b>{tag_slug.title()} Markets</b> (Page {page + 1})\n\n" + format_market_list(markets, start_num)
        keyboard = build_pm_category_markets(markets, tag_slug, page=page)

        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception as e:
        logger.error(f"Error getting category markets: {e}")
        await callback.answer(f"Error: {str(e)[:50]}", show_alert=True)


@router.callback_query(PMMarketAction.filter(F.action == "view"))
async def callback_pm_market_view(
    callback: CallbackQuery,
    callback_data: PMMarketAction,
    polymarket_indexer: Optional[PolymarketIndexer] = None,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Show market detail view."""
    await callback.answer("Loading market...")

    if not polymarket_indexer:
        await callback.answer("Polymarket not available", show_alert=True)
        return

    market_id = callback_data.market_id
    page = callback_data.page

    try:
        client = polymarket_indexer.client

        # Look up full market ID from Redis if available
        full_id = market_id
        if redis_client:
            cached_id = await redis_client.redis.get(f"pm:market_id:{market_id}")
            if cached_id:
                full_id = cached_id

        # Try to get market by condition_id or slug
        market = await client.get_market_by_condition_id(full_id)
        if not market:
            market = await client.get_market_by_slug(full_id)

        if not market:
            await callback.answer("Market not found", show_alert=True)
            return

        # Cache the mapping
        if redis_client and market.condition_id:
            await redis_client.redis.set(
                f"pm:market_id:{market_id}",
                market.condition_id,
                ex=86400,
            )

        text = format_market_detail(market)
        keyboard = build_pm_market_detail(market, page)

        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception as e:
        logger.error(f"Error viewing market: {e}")
        await callback.answer(f"Error: {str(e)[:50]}", show_alert=True)


@router.callback_query(PMMarketAction.filter(F.action == "positions"))
async def callback_pm_market_positions(
    callback: CallbackQuery,
    callback_data: PMMarketAction,
    polymarket_indexer: Optional[PolymarketIndexer] = None,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Show market positions/holders."""
    await callback.answer("Loading positions...")

    if not polymarket_indexer:
        await callback.answer("Polymarket not available", show_alert=True)
        return

    market_id = callback_data.market_id
    page = callback_data.page

    try:
        client = polymarket_indexer.client

        # Get full ID from cache
        full_id = market_id
        if redis_client:
            cached_id = await redis_client.redis.get(f"pm:market_id:{market_id}")
            if cached_id:
                full_id = cached_id

        # Get positions
        holders = await client.get_market_positions(full_id, limit=20)

        if not holders:
            await callback.answer("No positions found", show_alert=True)
            return

        # Group by outcome
        yes_holders = [h for h in holders if h.outcome.lower() == "yes"]
        no_holders = [h for h in holders if h.outcome.lower() == "no"]

        lines = ["👥 <b>Top Positions</b>\n"]

        if yes_holders:
            lines.append("<b>YES Holders:</b>")
            for i, h in enumerate(yes_holders[:5], 1):
                addr_short = shorten_address(h.address)
                lines.append(f"{i}. {addr_short} - ${h.value:,.0f}")

        if no_holders:
            lines.append("\n<b>NO Holders:</b>")
            for i, h in enumerate(no_holders[:5], 1):
                addr_short = shorten_address(h.address)
                lines.append(f"{i}. {addr_short} - ${h.value:,.0f}")

        text = "\n".join(lines)
        keyboard = build_pm_positions_keyboard(market_id, page)

        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception as e:
        logger.error(f"Error getting positions: {e}")
        await callback.answer(f"Error: {str(e)[:50]}", show_alert=True)


@router.callback_query(PMMarketAction.filter(F.action == "news"))
async def callback_pm_market_news(
    callback: CallbackQuery,
    callback_data: PMMarketAction,
    polymarket_indexer: Optional[PolymarketIndexer] = None,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Show news about market topic."""
    await callback.answer("Fetching news...")

    if not polymarket_indexer:
        await callback.answer("Polymarket not available", show_alert=True)
        return

    market_id = callback_data.market_id
    page = callback_data.page

    try:
        client = polymarket_indexer.client

        # Get market to extract topic
        full_id = market_id
        if redis_client:
            cached_id = await redis_client.redis.get(f"pm:market_id:{market_id}")
            if cached_id:
                full_id = cached_id

        market = await client.get_market_by_condition_id(full_id)
        if not market:
            market = await client.get_market_by_slug(full_id)

        if not market:
            await callback.answer("Market not found", show_alert=True)
            return

        # Extract key terms from market question
        question = market.question
        # Simple extraction - first few meaningful words
        words = [w for w in question.split() if len(w) > 3 and w.lower() not in ("will", "what", "when", "does", "the", "for")][:3]
        topic = " ".join(words)
        topic_google = topic.replace(" ", "+")
        topic_twitter = topic.replace(" ", "%20")

        # For now, show placeholder - real news would require web search integration
        lines = [
            f"📰 <b>News: {topic}</b>\n",
            "━━━━━━━━━━━━━━━━━━━━\n",
            "<i>News integration coming soon!</i>\n",
            f"Search for '{topic}' news:",
            f"• <a href='https://news.google.com/search?q={topic_google}'>Google News</a>",
            f"• <a href='https://twitter.com/search?q={topic_twitter}'>X/Twitter</a>",
        ]

        text = "\n".join(lines)
        keyboard = build_pm_news_keyboard(market_id, page)

        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True
        )

    except Exception as e:
        logger.error(f"Error getting news: {e}")
        await callback.answer(f"Error: {str(e)[:50]}", show_alert=True)


@router.callback_query(PMSearchAction.filter(F.action == "results"))
async def callback_pm_search_paginate(
    callback: CallbackQuery,
    callback_data: PMSearchAction,
    polymarket_indexer: Optional[PolymarketIndexer] = None,
) -> None:
    """Paginate search results."""
    await callback.answer()

    if not polymarket_indexer:
        await callback.answer("Polymarket not available", show_alert=True)
        return

    query = callback_data.query
    page = callback_data.page
    per_page = 10
    offset = page * per_page

    try:
        client = polymarket_indexer.client
        # Note: Search doesn't support offset, so we fetch more and slice
        markets = await client.search_markets(query, limit=100)
        markets = markets[offset : offset + per_page]

        if not markets:
            await callback.answer("No more results", show_alert=True)
            return

        start_num = offset + 1
        text = f"🔍 <b>Search: {query}</b> (Page {page + 1})\n\n" + format_market_list(markets, start_num)
        keyboard = build_pm_search_results(markets, query, page=page)

        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception as e:
        logger.error(f"Error paginating search: {e}")
        await callback.answer(f"Error: {str(e)[:50]}", show_alert=True)
