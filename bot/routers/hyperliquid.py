"""Hyperliquid commands router."""

import logging
from decimal import Decimal
from typing import Optional, List, Dict, Any

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command, CommandObject

from shared.utils import shorten_address, is_valid_ethereum_address, escape_html
from shared.formatting import format_usd, format_percent, format_pnl
from indexers.hyperliquid import HyperliquidClient, Direction
from bot.services.position_indexer import PositionIndexer
from bot.services.analytics_service import AnalyticsService, format_trader_analytics
from bot.keyboards.analytics import (
    TraderAction,
    WatchlistAction,
    CopyTradingAction,
    build_trader_actions_keyboard,
)
from bot.utils.filters import parse_filter_args, format_filter_summary
from bot.storage.redis_client import RedisClient

logger = logging.getLogger(__name__)

router = Router(name="hyperliquid")


def format_usd_short(value: Decimal | float) -> str:
    """Format USD value in short form (K/M)."""
    return format_usd(value, short=True)


@router.message(Command("hl_trades"))
async def cmd_hl_trades(
    message: Message,
    command: CommandObject,
    hyperliquid_client: Optional[HyperliquidClient] = None,
) -> None:
    """Show recent trades for a coin with optional size filter.

    Usage: /hl_trades <coin> [filter]
    Filter examples:
      /hl_trades BTC          - all BTC trades
      /hl_trades ETH >100k    - ETH trades above $100k
      /hl_trades SOL 50k-500k - SOL trades between $50k-$500k
    """
    if not command.args:
        await message.reply(
            "<b>Usage:</b> /hl_trades &lt;coin&gt; [filter]\n\n"
            "<b>Examples:</b>\n"
            "<code>/hl_trades BTC</code>\n"
            "<code>/hl_trades ETH >100k</code>\n"
            "<code>/hl_trades SOL 50k-500k</code>",
            parse_mode="HTML",
        )
        return

    if not hyperliquid_client:
        await message.reply("Hyperliquid client is not available.")
        return

    # Parse coin and optional size filter
    parts = command.args.strip().split()
    coin = parts[0].upper()
    min_size, max_size = None, None

    if len(parts) > 1:
        filter_str = " ".join(parts[1:])
        _, min_size, max_size = parse_filter_args(filter_str)

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        # Fetch more if filtering
        fetch_limit = 100 if min_size or max_size else 15
        trades = await hyperliquid_client.get_recent_trades(coin, fetch_limit)

        # Apply size filters
        if min_size is not None:
            trades = [t for t in trades if float(t.notional_usd) >= min_size]
        if max_size is not None:
            trades = [t for t in trades if float(t.notional_usd) <= max_size]

        if not trades:
            filter_desc = format_filter_summary(coin, min_size, max_size)
            await message.reply(
                f"No trades found matching: {filter_desc}",
                parse_mode="HTML",
            )
            return

        filter_desc = format_filter_summary(None, min_size, max_size) if min_size or max_size else "all"
        lines = [
            f"<b>Recent {coin} Trades</b>",
            f"<i>Filter: {filter_desc} ({len(trades)} trades)</i>\n",
        ]

        for trade in trades[:15]:
            dir_emoji = "🟢" if trade.direction == Direction.LONG else "🔴"
            dir_text = "LONG" if trade.direction == Direction.LONG else "SHORT"
            notional = format_usd_short(trade.notional_usd)
            address = trade.users[0]

            lines.append(
                f"{dir_emoji} <b>{dir_text}</b> {notional}\n"
                f"   {float(trade.size):,.4f} @ ${float(trade.price):,.2f}\n"
                f"   <code>{address}</code>"
            )

        if len(trades) > 15:
            lines.append(f"\n<i>... and {len(trades) - 15} more</i>")

        await message.reply("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error getting trades for {coin}: {e}")
        await message.reply(f"Error fetching trades: {str(e)}")


@router.message(Command("hl_last_trades"))
async def cmd_hl_last_trades(
    message: Message,
    command: CommandObject,
    hyperliquid_client: Optional[HyperliquidClient] = None,
) -> None:
    """Show last N trades for a coin.

    Usage: /hl_last_trades <coin> [count]
    Count options: 10, 20, 50 (default: 10)
    """
    if not command.args:
        await message.reply(
            "<b>Usage:</b> /hl_last_trades &lt;coin&gt; [count]\n\n"
            "<b>Examples:</b>\n"
            "<code>/hl_last_trades BTC</code> - last 10 BTC trades\n"
            "<code>/hl_last_trades ETH 20</code> - last 20 ETH trades\n"
            "<code>/hl_last_trades SOL 50</code> - last 50 SOL trades",
            parse_mode="HTML",
        )
        return

    if not hyperliquid_client:
        await message.reply("Hyperliquid client is not available.")
        return

    parts = command.args.strip().split()
    coin = parts[0].upper()

    # Parse count (default 10, max 50)
    count = 10
    if len(parts) > 1:
        try:
            count = min(int(parts[1]), 50)
        except ValueError:
            count = 10

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        trades = await hyperliquid_client.get_recent_trades(coin, count)

        if not trades:
            await message.reply(f"No trades found for {coin}", parse_mode="HTML")
            return

        lines = [
            f"<b>Last {len(trades)} {coin} Trades</b>\n",
        ]

        for trade in trades:
            dir_emoji = "🟢" if trade.direction == Direction.LONG else "🔴"
            dir_text = "LONG" if trade.direction == Direction.LONG else "SHORT"
            notional = format_usd_short(trade.notional_usd)
            address = trade.users[0]

            lines.append(
                f"{dir_emoji} <b>{dir_text}</b> {notional}\n"
                f"   {float(trade.size):,.4f} @ ${float(trade.price):,.2f}\n"
                f"   <code>{address}</code>"
            )

        await message.reply("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error getting last trades for {coin}: {e}")
        await message.reply(f"Error fetching trades: {str(e)}")


@router.message(Command("hl_sentiment"))
async def cmd_hl_sentiment(
    message: Message,
    command: CommandObject,
    hyperliquid_client: Optional[HyperliquidClient] = None,
) -> None:
    """Show market sentiment for a coin.

    Usage: /hl_sentiment <coin> [hours]
    """
    if not command.args:
        await message.reply(
            "<b>Usage:</b> /hl_sentiment &lt;coin&gt; [hours]\n\n"
            "<b>Examples:</b>\n"
            "<code>/hl_sentiment BTC</code>\n"
            "<code>/hl_sentiment ETH 24</code>",
            parse_mode="HTML",
        )
        return

    if not hyperliquid_client:
        await message.reply("Hyperliquid client is not available.")
        return

    parts = command.args.strip().upper().split()
    coin = parts[0]
    hours = float(parts[1]) if len(parts) > 1 else 12

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        sentiment = await hyperliquid_client.compute_sentiment(coin, hours)

        # Sentiment emoji
        if sentiment.sentiment == "Bullish":
            emoji = "🟢"
        elif sentiment.sentiment == "Bearish":
            emoji = "🔴"
        else:
            emoji = "⚪"

        # Volume bar
        total_vol = sentiment.long_volume + sentiment.short_volume
        if total_vol > 0:
            long_bar_len = int((sentiment.long_volume / total_vol) * 10)
            short_bar_len = 10 - long_bar_len
            vol_bar = "🟩" * long_bar_len + "🟥" * short_bar_len
        else:
            vol_bar = "⬜" * 10

        lines = [
            f"{emoji} <b>{coin} Market Sentiment</b>",
            f"<i>Last {hours:.0f} hours</i>\n",
            f"<b>Sentiment:</b> {sentiment.sentiment}",
            f"<b>Total Trades:</b> {sentiment.total_trades:,}\n",
            f"<b>Longs:</b> {sentiment.long_count:,} ({sentiment.long_pct:.1f}%)",
            f"<b>Shorts:</b> {sentiment.short_count:,} ({sentiment.short_pct:.1f}%)\n",
            f"<b>Volume:</b>",
            f"{vol_bar}",
            f"🟢 {format_usd_short(sentiment.long_volume)} | 🔴 {format_usd_short(sentiment.short_volume)}",
        ]

        await message.reply("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error getting sentiment for {coin}: {e}")
        await message.reply(f"Error fetching sentiment: {str(e)}")


@router.message(Command("hl_positions"))
async def cmd_hl_positions(
    message: Message,
    command: CommandObject,
    hyperliquid_client: Optional[HyperliquidClient] = None,
) -> None:
    """Show positions for an address.

    Usage: /hl_positions <address>
    """
    if not command.args:
        await message.reply(
            "<b>Usage:</b> /hl_positions &lt;address&gt;\n\n"
            "<b>Example:</b>\n"
            "<code>/hl_positions 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb</code>",
            parse_mode="HTML",
        )
        return

    address = command.args.strip()

    if not is_valid_ethereum_address(address):
        await message.reply("Invalid Ethereum address. Please provide a valid 0x address.")
        return

    if not hyperliquid_client:
        await message.reply("Hyperliquid client is not available.")
        return

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        state = await hyperliquid_client.get_user_state(address)

        if not state.positions:
            await message.reply(
                f"No open positions for <code>{shorten_address(address)}</code>",
                parse_mode="HTML",
            )
            return

        # Summary
        total_pnl = state.total_pnl
        pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"

        lines = [
            f"<b>Hyperliquid Positions</b>",
            f"<code>{shorten_address(address)}</code>\n",
            f"<b>Account Value:</b> ${float(state.account_value):,.2f}",
            f"<b>Margin Used:</b> ${float(state.total_margin_used):,.2f}",
            f"{pnl_emoji} <b>Total PnL:</b> {format_usd(total_pnl, sign=True)}\n",
            f"<b>Positions ({len(state.positions)}):</b>",
        ]

        for pos in state.positions[:10]:
            dir_emoji = "🟢" if pos.direction == Direction.LONG else "🔴"
            dir_text = "LONG" if pos.direction == Direction.LONG else "SHORT"
            roe_pct = float(pos.return_on_equity) * 100
            pnl_str = format_pnl(pos.unrealized_pnl, roe_pct)

            leverage_str = f" {float(pos.leverage):.0f}x" if pos.leverage else ""

            lines.append(
                f"\n{dir_emoji} <b>{pos.coin}</b> {dir_text}{leverage_str}\n"
                f"   Size: {float(pos.abs_size):,.4f} (${float(pos.position_value):,.0f})\n"
                f"   Entry: ${float(pos.entry_px):,.2f}\n"
                f"   PnL: {pnl_str}"
            )

        if len(state.positions) > 10:
            lines.append(f"\n<i>... and {len(state.positions) - 10} more positions</i>")

        await message.reply("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error getting positions for {address}: {e}")
        await message.reply(f"Error fetching positions: {str(e)}")


@router.message(Command("hl_pnl"))
async def cmd_hl_pnl(
    message: Message,
    command: CommandObject,
    hyperliquid_client: Optional[HyperliquidClient] = None,
) -> None:
    """Show PnL summary for an address.

    Usage: /hl_pnl <address>
    """
    if not command.args:
        await message.reply(
            "<b>Usage:</b> /hl_pnl &lt;address&gt;\n\n"
            "<b>Example:</b>\n"
            "<code>/hl_pnl 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb</code>",
            parse_mode="HTML",
        )
        return

    address = command.args.strip()

    if not is_valid_ethereum_address(address):
        await message.reply("Invalid Ethereum address.")
        return

    if not hyperliquid_client:
        await message.reply("Hyperliquid client is not available.")
        return

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        # Get current state and recent fills
        state = await hyperliquid_client.get_user_state(address)
        fills = await hyperliquid_client.get_user_fills(address, limit=100)

        # Calculate realized PnL from recent fills
        realized_pnl = sum((f.closed_pnl for f in fills), Decimal("0"))
        unrealized_pnl = state.total_pnl
        total_pnl = realized_pnl + unrealized_pnl

        pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"

        # Win/loss stats from fills
        winning = [f for f in fills if f.closed_pnl > 0]
        losing = [f for f in fills if f.closed_pnl < 0]
        win_rate = (len(winning) / len(fills) * 100) if fills else 0

        lines = [
            f"<b>Hyperliquid PnL</b>",
            f"<code>{shorten_address(address)}</code>\n",
            f"<b>Account Value:</b> ${float(state.account_value):,.2f}",
            f"<b>Withdrawable:</b> ${float(state.withdrawable):,.2f}\n",
            f"{pnl_emoji} <b>Total PnL:</b> {format_usd(total_pnl, sign=True)}",
            f"<b>Realized:</b> {format_usd(realized_pnl, sign=True)}",
            f"<b>Unrealized:</b> {format_usd(unrealized_pnl, sign=True)}\n",
            f"<b>Recent Trades:</b> {len(fills)}",
            f"<b>Win Rate:</b> {win_rate:.1f}%",
            f"<b>W/L:</b> {len(winning)}W / {len(losing)}L",
        ]

        if fills:
            # Show largest win/loss
            best = max(fills, key=lambda f: f.closed_pnl)
            worst = min(fills, key=lambda f: f.closed_pnl)

            if best.closed_pnl > 0:
                lines.append(f"\n🏆 <b>Best:</b> {best.coin} {format_usd(best.closed_pnl, sign=True)}")
            if worst.closed_pnl < 0:
                lines.append(f"💀 <b>Worst:</b> {worst.coin} {format_usd(worst.closed_pnl, sign=True)}")

        await message.reply("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error getting PnL for {address}: {e}")
        await message.reply(f"Error fetching PnL: {str(e)}")


@router.message(Command("hl_fills"))
async def cmd_hl_fills(
    message: Message,
    command: CommandObject,
    hyperliquid_client: Optional[HyperliquidClient] = None,
) -> None:
    """Show recent trade fills for an address.

    Usage: /hl_fills <address> [limit]
    """
    if not command.args:
        await message.reply(
            "<b>Usage:</b> /hl_fills &lt;address&gt; [limit]\n\n"
            "<b>Example:</b>\n"
            "<code>/hl_fills 0x742d... 20</code>",
            parse_mode="HTML",
        )
        return

    parts = command.args.strip().split()
    address = parts[0]
    limit = min(int(parts[1]), 50) if len(parts) > 1 else 10

    if not is_valid_ethereum_address(address):
        await message.reply("Invalid Ethereum address.")
        return

    if not hyperliquid_client:
        await message.reply("Hyperliquid client is not available.")
        return

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        fills = await hyperliquid_client.get_user_fills(address, limit=limit)

        if not fills:
            await message.reply(
                f"No recent fills for <code>{shorten_address(address)}</code>",
                parse_mode="HTML",
            )
            return

        lines = [
            f"<b>Recent Fills</b>",
            f"<code>{shorten_address(address)}</code>\n",
        ]

        for fill in fills[:limit]:
            # Determine emoji based on direction
            if "Long" in fill.direction:
                dir_emoji = "🟢"
            elif "Short" in fill.direction:
                dir_emoji = "🔴"
            else:
                dir_emoji = "⚪"

            time_str = fill.datetime.strftime("%m/%d %H:%M")
            notional = format_usd_short(fill.notional_usd)
            pnl_str = f" | PnL: {format_usd(fill.closed_pnl, sign=True)}" if fill.closed_pnl != 0 else ""

            lines.append(
                f"{dir_emoji} <b>{fill.coin}</b> {escape_html(fill.direction)}\n"
                f"   {float(fill.size):,.4f} @ ${float(fill.price):,.2f} ({notional}){pnl_str}\n"
                f"   {time_str}"
            )

        if len(fills) > limit:
            lines.append(f"\n<i>... and more fills</i>")

        await message.reply("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error getting fills for {address}: {e}")
        await message.reply(f"Error fetching fills: {str(e)}")


@router.message(Command("hl_whales"))
async def cmd_hl_whales(
    message: Message,
    command: CommandObject,
    hyperliquid_client: Optional[HyperliquidClient] = None,
) -> None:
    """Show recent whale trades on Hyperliquid with optional filters.

    Usage: /hl_whales [filter]
    Filter examples:
      /hl_whales              - trades above $1M (default)
      /hl_whales 5m           - trades above $5M
      /hl_whales >10m         - trades above $10M
      /hl_whales <5m          - trades below $5M
      /hl_whales btc          - filter by coin
      /hl_whales eth >5m      - combine coin and size filter
    """
    min_size = 1000000.0  # $1M default
    max_size = None
    coin_filter = None

    if command.args:
        coin_filter, parsed_min, parsed_max = parse_filter_args(command.args)
        if parsed_min is not None:
            min_size = parsed_min
        if parsed_max is not None:
            max_size = parsed_max
        # If coin filter looks like a size (e.g., just "5m"), treat it as size
        if coin_filter and coin_filter.lower().replace('.', '').replace('m', '').replace('k', '').isdigit():
            try:
                parsed = float(coin_filter.lower().replace('m', '')) * 1_000_000 if 'm' in coin_filter.lower() else float(coin_filter.lower().replace('k', '')) * 1000 if 'k' in coin_filter.lower() else float(coin_filter)
                min_size = parsed
                coin_filter = None
            except ValueError:
                pass

    if not hyperliquid_client:
        await message.reply("Hyperliquid client is not available.")
        return

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        # Fetch more trades if filtering
        fetch_limit = 30 if coin_filter or max_size else 15
        trades = await hyperliquid_client.get_whale_trades(threshold=Decimal(str(min_size)), limit=fetch_limit)

        # Apply additional filters
        if max_size is not None:
            trades = [t for t in trades if float(t.notional_usd) <= max_size]

        if coin_filter:
            coin_upper = coin_filter.upper()
            trades = [t for t in trades if coin_upper in t.coin.upper()]

        if not trades:
            filter_desc = format_filter_summary(coin_filter, min_size, max_size)
            await message.reply(
                f"No whale trades found matching: {filter_desc}",
                parse_mode="HTML",
            )
            return

        filter_desc = format_filter_summary(coin_filter, min_size, max_size)
        lines = [
            f"<b>🐋 Hyperliquid Whale Trades</b>",
            f"<i>Filter: {filter_desc}</i>\n",
        ]

        for trade in trades[:15]:
            dir_emoji = "🟢" if trade.direction == Direction.LONG else "🔴"
            dir_text = "LONG" if trade.direction == Direction.LONG else "SHORT"
            address = trade.users[0]

            lines.append(
                f"{dir_emoji} <b>{format_usd_short(trade.notional_usd)}</b> {trade.coin} {dir_text}\n"
                f"   {float(trade.size):,.4f} @ ${float(trade.price):,.2f}\n"
                f"   <code>{address}</code>"
            )

        if len(trades) > 15:
            lines.append(f"\n<i>... and {len(trades) - 15} more</i>")

        await message.reply("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error getting whale trades: {e}")
        await message.reply(f"Error fetching whale trades: {str(e)}")


@router.message(Command("hl_market"))
async def cmd_hl_market(
    message: Message,
    command: CommandObject,
    hyperliquid_client: Optional[HyperliquidClient] = None,
) -> None:
    """Show market info for a coin.

    Usage: /hl_market <coin>
    """
    if not command.args:
        await message.reply(
            "<b>Usage:</b> /hl_market &lt;coin&gt;\n\n"
            "<b>Example:</b>\n"
            "<code>/hl_market BTC</code>",
            parse_mode="HTML",
        )
        return

    if not hyperliquid_client:
        await message.reply("Hyperliquid client is not available.")
        return

    coin = command.args.strip().upper()

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        contexts = await hyperliquid_client.get_asset_contexts()

        if coin not in contexts:
            await message.reply(f"Coin <b>{coin}</b> not found.", parse_mode="HTML")
            return

        ctx = contexts[coin]

        # Price change emoji
        change = ctx.price_change_24h
        if change and change > 0:
            change_emoji = "🟢"
        elif change and change < 0:
            change_emoji = "🔴"
        else:
            change_emoji = "⚪"
            change = Decimal("0")

        lines = [
            f"<b>{coin} Market Info</b>\n",
            f"<b>Mark Price:</b> ${float(ctx.mark_px or 0):,.2f}",
            f"<b>24h Change:</b> {change_emoji} {format_percent(change)}",
            f"<b>24h Volume:</b> {format_usd_short(ctx.day_ntl_vlm or 0)}",
            f"<b>Open Interest:</b> {format_usd_short(ctx.open_interest or 0)}",
        ]

        if ctx.funding_rate:
            funding_pct = float(ctx.funding_rate) * 100
            funding_emoji = "🟢" if funding_pct > 0 else "🔴" if funding_pct < 0 else "⚪"
            lines.append(f"<b>Funding Rate:</b> {funding_emoji} {funding_pct:.4f}%")

        await message.reply("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error getting market info for {coin}: {e}")
        await message.reply(f"Error fetching market info: {str(e)}")


@router.message(Command("hl_coins"))
async def cmd_hl_coins(
    message: Message,
    hyperliquid_client: Optional[HyperliquidClient] = None,
) -> None:
    """List available coins on Hyperliquid."""
    if not hyperliquid_client:
        await message.reply("Hyperliquid client is not available.")
        return

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        coins = await hyperliquid_client.get_coins()

        lines = [
            f"<b>Hyperliquid Coins</b>",
            f"<i>{len(coins)} available</i>\n",
            ", ".join(coins[:50]),
        ]

        if len(coins) > 50:
            lines.append(f"\n<i>... and {len(coins) - 50} more</i>")

        await message.reply("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error getting coins: {e}")
        await message.reply(f"Error fetching coins: {str(e)}")


def _format_usd_short(value: float) -> str:
    """Compact $ format for leaderboard display."""
    abs_v = abs(value)
    sign = "-" if value < 0 else ""
    if abs_v >= 1e9:
        return f"{sign}${abs_v / 1e9:.2f}B"
    if abs_v >= 1e6:
        return f"{sign}${abs_v / 1e6:.2f}M"
    if abs_v >= 1e3:
        return f"{sign}${abs_v / 1e3:.2f}K"
    return f"{sign}${abs_v:.0f}"


@router.message(Command("top"))
async def cmd_top(
    message: Message,
    command: CommandObject,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Show top HL traders straight from HL's own leaderboard.

    Usage: /top [N]         — top N (default 10, max 50)
    Source: stats-data.hyperliquid.xyz/Mainnet/leaderboard, refreshed every 5 min.
    """
    if not redis_client:
        await message.reply("Storage unavailable.")
        return

    n = 10
    if command.args:
        try:
            n = max(1, min(50, int(command.args.strip())))
        except ValueError:
            pass

    addresses = await redis_client.zrange("hl:top_traders", 0, n - 1, desc=True)
    if not addresses:
        await message.reply(
            "Top-trader crawler has no data yet. "
            "The indexer populates this every 5 minutes — try again shortly.",
        )
        return

    pipe = redis_client.redis.pipeline(transaction=False)
    for addr in addresses:
        pipe.hgetall(f"hl:top_trader:{addr}")
    results = await pipe.execute()

    updated_at = await redis_client.get("hl:top_traders:updated_at")
    try:
        age_s = int(__import__("time").time() - int(updated_at)) if updated_at else None
    except (TypeError, ValueError):
        age_s = None

    lines = [f"<b>🏆 HL Top {n} (by all-time PnL)</b>"]
    if age_s is not None:
        lines.append(f"<i>Updated {age_s}s ago</i>")
    lines.append("")
    for idx, (addr, fields) in enumerate(zip(addresses, results), start=1):
        if not fields:
            continue
        name = fields.get("display_name") or shorten_address(addr)
        pnl = float(fields.get("allTime_pnl", 0) or 0)
        roi = float(fields.get("allTime_roi", 0) or 0)
        av = float(fields.get("account_value", 0) or 0)
        lines.append(
            f"<b>{idx}.</b> <code>{escape_html(name)}</code> "
            f"— {_format_usd_short(pnl)} "
            f"({roi * 100:+.1f}%) | AV {_format_usd_short(av)}",
        )

    await message.reply("\n".join(lines), parse_mode="HTML")


def _parse_timeseries_member(member: str) -> tuple[int, float] | None:
    """Members are stored as `{ts_ms}:{value}` by MarketStatsIndexer."""
    try:
        ts_str, val_str = member.split(":", 1)
        return int(ts_str), float(val_str)
    except (ValueError, AttributeError):
        return None


async def _latest_timeseries(redis: RedisClient, key: str, n: int) -> list[tuple[int, float]]:
    raw = await redis.redis.zrevrange(key, 0, n - 1)
    points: list[tuple[int, float]] = []
    for m in raw or []:
        parsed = _parse_timeseries_member(m)
        if parsed is not None:
            points.append(parsed)
    return points


@router.message(Command("funding"))
async def cmd_funding(
    message: Message,
    command: CommandObject,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Recent funding-rate samples for a coin.

    Usage: /funding BTC
    """
    if not redis_client:
        await message.reply("Storage unavailable.")
        return
    if not command.args:
        await message.reply(
            "<b>Usage:</b> /funding &lt;coin&gt;\n\nExample: <code>/funding BTC</code>",
            parse_mode="HTML",
        )
        return

    coin = command.args.strip().split()[0].upper()
    points = await _latest_timeseries(redis_client, f"hl:funding:{coin}", 10)
    if not points:
        await message.reply(
            f"No funding history indexed for <b>{escape_html(coin)}</b> yet. "
            f"The market-stats indexer snapshots every 60s.",
            parse_mode="HTML",
        )
        return

    current_ts, current = points[0]
    vals = [v for _, v in points]
    lines = [
        f"<b>📊 {escape_html(coin)} Funding Rate</b>",
        f"Current: <code>{current * 100:+.4f}%</code>/hr",
        f"Range in last {len(points)} samples: "
        f"<code>{min(vals) * 100:+.4f}%</code> → <code>{max(vals) * 100:+.4f}%</code>",
        "",
    ]
    for ts_ms, v in points:
        age_s = max(0, int(__import__("time").time() - ts_ms / 1000))
        lines.append(f"  −{age_s:>4}s   <code>{v * 100:+.4f}%</code>")

    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("oi"))
async def cmd_oi(
    message: Message,
    command: CommandObject,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Recent open-interest samples for a coin.

    Usage: /oi BTC
    """
    if not redis_client:
        await message.reply("Storage unavailable.")
        return
    if not command.args:
        await message.reply(
            "<b>Usage:</b> /oi &lt;coin&gt;\n\nExample: <code>/oi BTC</code>",
            parse_mode="HTML",
        )
        return

    coin = command.args.strip().split()[0].upper()
    points = await _latest_timeseries(redis_client, f"hl:oi:{coin}", 10)
    if not points:
        await message.reply(
            f"No OI history indexed for <b>{escape_html(coin)}</b> yet.",
            parse_mode="HTML",
        )
        return

    current = points[0][1]
    vals = [v for _, v in points]
    lines = [
        f"<b>📈 {escape_html(coin)} Open Interest</b>",
        f"Current: {_format_usd_short(current)}",
        f"Range in last {len(points)} samples: "
        f"{_format_usd_short(min(vals))} → {_format_usd_short(max(vals))}",
        "",
    ]
    for ts_ms, v in points:
        age_s = max(0, int(__import__("time").time() - ts_ms / 1000))
        lines.append(f"  −{age_s:>4}s   {_format_usd_short(v)}")

    await message.reply("\n".join(lines), parse_mode="HTML")


_HLP_ADDRESS = "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"


def _parse_snapshot_member(member: str) -> tuple[int, float, int] | None:
    """Vault snapshot members are `{ts_ms}:{tvl}:{follower_count}`."""
    try:
        ts, tvl, fc = member.split(":", 2)
        return int(ts), float(tvl), int(fc)
    except (ValueError, AttributeError):
        return None


async def _render_vault_card(
    redis_client: RedisClient, address: str, n_snapshots: int = 5,
) -> str:
    summary = await redis_client.redis.hgetall(f"hl:vault:{address}:summary")
    if not summary:
        return f"No summary indexed for <code>{escape_html(shorten_address(address))}</code>."

    name = summary.get("name") or shorten_address(address)
    tvl = float(summary.get("tvl") or 0)
    apr = float(summary.get("apr") or 0)
    leader = summary.get("leader") or ""
    is_closed = summary.get("is_closed") == "1"

    lines = [
        f"<b>🏦 {escape_html(name)}</b>",
        f"TVL: {_format_usd_short(tvl)}",
        f"APR: {apr * 100:+.2f}%",
    ]
    if leader:
        lines.append(f"Leader: <code>{escape_html(shorten_address(leader))}</code>")
    lines.append(f"Address: <code>{escape_html(address)}</code>")
    if is_closed:
        lines.append("<i>⚠️ closed to new deposits</i>")

    snaps_raw = await redis_client.redis.zrevrange(
        f"hl:vault:{address}:snapshots", 0, n_snapshots - 1,
    )
    snaps = [p for p in (_parse_snapshot_member(m) for m in (snaps_raw or [])) if p]
    if len(snaps) >= 2:
        recent = snaps[0][1]
        older = snaps[-1][1]
        delta = recent - older
        lines.append("")
        lines.append(
            f"Recent {len(snaps)} snapshots delta: "
            f"<code>{delta:+,.0f}</code> "
            f"({(delta / older) * 100:+.2f}%)"
            if older > 0 else f"Recent delta: <code>{delta:+,.0f}</code>",
        )

    return "\n".join(lines)


@router.message(Command("vaults"))
async def cmd_vaults(
    message: Message,
    command: CommandObject,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Top HL vaults by TVL.

    Usage: /vaults [N]   (default 10, max 30)
    """
    if not redis_client:
        await message.reply("Storage unavailable.")
        return

    n = 10
    if command.args:
        try:
            n = max(1, min(30, int(command.args.strip().split()[0])))
        except ValueError:
            pass

    addresses = await redis_client.zrange("hl:vaults", 0, n - 1, desc=True)
    if not addresses:
        await message.reply(
            "Vault indexer has no data yet — runs every 5 minutes on startup.",
        )
        return

    pipe = redis_client.redis.pipeline(transaction=False)
    for addr in addresses:
        pipe.hgetall(f"hl:vault:{addr}:summary")
    rows = await pipe.execute()

    lines = [f"<b>🏦 HL Vaults Top {len(addresses)} by TVL</b>", ""]
    for i, (addr, data) in enumerate(zip(addresses, rows), start=1):
        if not data:
            continue
        name = data.get("name") or shorten_address(addr)
        tvl = float(data.get("tvl") or 0)
        apr = float(data.get("apr") or 0)
        lines.append(
            f"<b>{i}.</b> <code>{escape_html(name[:30])}</code> "
            f"— {_format_usd_short(tvl)} ({apr * 100:+.1f}% APR)",
        )

    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("hlp"))
async def cmd_hlp(
    message: Message,
    command: CommandObject,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Hyperliquidity Provider (HLP) overview."""
    if not redis_client:
        await message.reply("Storage unavailable.")
        return
    text = await _render_vault_card(redis_client, _HLP_ADDRESS, n_snapshots=10)
    await message.reply(text, parse_mode="HTML")


@router.message(Command("vault"))
async def cmd_vault(
    message: Message,
    command: CommandObject,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Details for a specific vault.

    Usage: /vault 0xABC...
    """
    if not redis_client:
        await message.reply("Storage unavailable.")
        return
    if not command.args:
        await message.reply(
            "<b>Usage:</b> /vault &lt;address&gt;\n\n"
            "Try <code>/vaults</code> to discover addresses.",
            parse_mode="HTML",
        )
        return

    arg = command.args.strip().split()[0]
    if not (arg.startswith("0x") and len(arg) == 42):
        await message.reply("Invalid vault address.")
        return
    text = await _render_vault_card(redis_client, arg.lower(), n_snapshots=5)
    await message.reply(text, parse_mode="HTML")


@router.message(Command("liquidations"))
async def cmd_liquidations(
    message: Message,
    command: CommandObject,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Recent HL liquidations.

    Usage:
      /liquidations            — last 10
      /liquidations 25         — last 25 (max 50)
      /liquidations 0xABC...   — per-wallet history (last 10)
    """
    if not redis_client:
        await message.reply("Storage unavailable.")
        return

    args = (command.args or "").strip().split()
    n = 10
    address: Optional[str] = None

    for a in args:
        if a.startswith("0x") and len(a) == 42:
            address = a.lower()
        else:
            try:
                n = max(1, min(50, int(a)))
            except ValueError:
                pass

    key = f"hl:liq:addr:{address}" if address else "hl:liquidations"
    ids = await redis_client.zrange(key, 0, n - 1, desc=True)
    if not ids:
        scope = f"for <code>{escape_html(shorten_address(address))}</code>" if address else ""
        await message.reply(f"No liquidations found {scope}.", parse_mode="HTML")
        return

    pipe = redis_client.redis.pipeline(transaction=False)
    for liq_id in ids:
        pipe.hgetall(f"hl:liq:{liq_id}")
    rows = await pipe.execute()

    header = "<b>💥 Recent Liquidations</b>"
    if address:
        header = f"<b>💥 Liquidations for</b> <code>{escape_html(shorten_address(address))}</code>"

    lines = [header, ""]
    for data in rows:
        if not data:
            continue
        addr = data.get("address", "")
        ntl = float(data.get("ntl_pos", 0) or 0)
        av = float(data.get("account_value", 0) or 0)
        lines.append(
            f"• <code>{escape_html(shorten_address(addr))}</code> "
            f"— {_format_usd_short(ntl)} pos"
            f" (acct {_format_usd_short(av)})",
        )

    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("clusters"))
async def cmd_clusters(
    message: Message,
    command: CommandObject,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Recent TWAP/accumulation clusters detected by the bot.

    Usage:
      /clusters            — last 10 globally
      /clusters 25         — last N (max 50)
      /clusters 0xABC...   — history for one wallet
    """
    if not redis_client:
        await message.reply("Storage unavailable.")
        return

    args = (command.args or "").strip().split()
    n = 10
    address: Optional[str] = None
    for a in args:
        if a.startswith("0x") and len(a) == 42:
            address = a.lower()
        else:
            try:
                n = max(1, min(50, int(a)))
            except ValueError:
                pass

    key = f"hl:clusters:{address}" if address else "hl:clusters"
    ids = await redis_client.zrange(key, 0, n - 1, desc=True)
    if not ids:
        scope = (
            f"for <code>{escape_html(shorten_address(address))}</code>"
            if address else ""
        )
        await message.reply(f"No clusters detected yet {scope}.", parse_mode="HTML")
        return

    pipe = redis_client.redis.pipeline(transaction=False)
    for cid in ids:
        pipe.hgetall(f"hl:cluster:{cid}")
    rows = await pipe.execute()

    header = "<b>🧱 Recent accumulation/distribution clusters</b>"
    if address:
        header = (
            f"<b>🧱 Clusters for</b> "
            f"<code>{escape_html(shorten_address(address))}</code>"
        )
    lines = [header, ""]
    for data in rows:
        if not data:
            continue
        coin = data.get("coin", "?")
        direction = data.get("direction", "?")
        notional = float(data.get("total_notional", 0) or 0)
        fills = int(float(data.get("fill_count", 0) or 0))
        duration = float(data.get("duration_seconds", 0) or 0)
        addr_short = shorten_address(data.get("address", ""))
        arrow = "⬆️" if direction == "accumulation" else "⬇️"
        lines.append(
            f"{arrow} <code>{escape_html(addr_short)}</code> "
            f"<b>{escape_html(coin)}</b> — "
            f"{_format_usd_short(notional)} "
            f"over {fills} fills / {int(duration)}s",
        )

    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("hl_indexed"))
async def cmd_hl_indexed(
    message: Message,
    command: CommandObject,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Show indexed Hyperliquid trades from Redis with filters.

    Usage: /hl_indexed [filter]
    Filter examples:
      /hl_indexed             - all recent trades
      /hl_indexed btc         - BTC trades only
      /hl_indexed >500k       - trades above $500k
      /hl_indexed eth 100k+   - combine coin and size filter
    """
    if not redis_client:
        await message.reply("Redis is not available. Indexer may not be running.")
        return

    # Parse filters
    coin_filter, min_size, max_size = None, None, None
    if command.args:
        coin_filter, min_size, max_size = parse_filter_args(command.args)

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        # Get recent trades from Redis
        tids = await redis_client.zrange("hl:trades", 0, 199, desc=True)

        if not tids:
            await message.reply("No indexed trades found. Indexer may not be running.")
            return

        trades = []
        for tid in tids:
            trade_data = await redis_client.redis.hgetall(f"hl:trade:{tid}")
            if trade_data:
                # Apply filters
                notional = float(trade_data.get("notional_usd", 0))
                coin = trade_data.get("coin", "")

                # Coin filter
                if coin_filter and coin_filter.upper() not in coin.upper():
                    continue

                # Size filter
                if min_size is not None and notional < min_size:
                    continue
                if max_size is not None and notional > max_size:
                    continue

                trades.append(trade_data)
                if len(trades) >= 15:
                    break

        if not trades:
            filter_desc = format_filter_summary(coin_filter, min_size, max_size)
            await message.reply(
                f"No indexed trades found matching: {filter_desc}",
                parse_mode="HTML",
            )
            return

        filter_desc = format_filter_summary(coin_filter, min_size, max_size)
        lines = [
            f"<b>📊 Indexed Hyperliquid Trades</b>",
            f"<i>Filter: {filter_desc}</i>\n",
        ]

        for trade in trades:
            notional = float(trade.get("notional_usd", 0))
            coin = trade.get("coin", "")
            side = trade.get("side", "")
            direction = trade.get("direction", "")
            price = float(trade.get("price", 0))
            size = float(trade.get("size", 0))
            time_str = trade.get("time_str", "")[-8:]  # Just time part

            is_long = direction == "long" or side == "B"
            dir_emoji = "🟢" if is_long else "🔴"
            dir_text = "LONG" if is_long else "SHORT"

            lines.append(
                f"{dir_emoji} <b>{coin}</b> {dir_text} {format_usd_short(notional)}\n"
                f"   {size:,.4f} @ ${price:,.2f}\n"
                f"   {time_str}"
            )

        await message.reply("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error getting indexed trades: {e}")
        await message.reply(f"Error fetching indexed trades: {str(e)}")


@router.message(Command("hl_analytics"))
async def cmd_hl_analytics(
    message: Message,
    command: CommandObject,
    hyperliquid_client: Optional[HyperliquidClient] = None,
    redis_client: Optional[RedisClient] = None,
    analytics_service: Optional[AnalyticsService] = None,
    wallet_service=None,
) -> None:
    """Show TrySuper-style trader analytics with action buttons.

    Usage: /hl_analytics <address>
    """
    if not command.args:
        await message.reply(
            "<b>Usage:</b> /hl_analytics &lt;address&gt;\n\n"
            "<b>Example:</b>\n"
            "<code>/hl_analytics 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb</code>\n\n"
            "Shows comprehensive trader metrics including:\n"
            "- Trader Type & Account Age\n"
            "- Profit Factor & Win Rate\n"
            "- ROE (7d/30d/90d)\n"
            "- Max Drawdown & Direction Bias",
            parse_mode="HTML",
        )
        return

    address = command.args.strip()

    if not is_valid_ethereum_address(address):
        await message.reply("Invalid Ethereum address.")
        return

    if not hyperliquid_client:
        await message.reply("Hyperliquid client is not available.")
        return

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        svc = analytics_service or AnalyticsService(hyperliquid_client, redis_client)
        analytics = await svc.get_trader_analytics(address, "hyperliquid")

        text = format_trader_analytics(analytics)

        is_tracked = False
        if wallet_service:
            is_tracked = await wallet_service.is_tracking(
                message.from_user.id, address
            )

        keyboard = build_trader_actions_keyboard(address, "hyperliquid", is_tracked)

        await message.reply(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception as e:
        logger.error(f"Error getting analytics for {address}: {e}")
        await message.reply(f"Error fetching analytics: {str(e)}")


# ============== Callback Handlers ==============

@router.callback_query(TraderAction.filter(F.action == "refresh"))
async def callback_refresh_analytics(
    callback: CallbackQuery,
    callback_data: TraderAction,
    hyperliquid_client: Optional[HyperliquidClient] = None,
    redis_client: Optional[RedisClient] = None,
    analytics_service: Optional[AnalyticsService] = None,
    wallet_service=None,
) -> None:
    """Refresh trader analytics."""
    await callback.answer("Refreshing...")

    if not hyperliquid_client:
        await callback.answer("Client not available", show_alert=True)
        return

    addr_short = callback_data.address
    address = addr_short

    if redis_client:
        full_addr = await redis_client.get(f"addr_map:{addr_short}")
        if full_addr:
            address = full_addr

    try:
        svc = analytics_service or AnalyticsService(hyperliquid_client, redis_client)
        analytics = await svc.get_trader_analytics(
            address, "hyperliquid", force_refresh=True
        )

        text = format_trader_analytics(analytics)

        is_tracked = False
        if wallet_service:
            is_tracked = await wallet_service.is_tracking(
                callback.from_user.id, address
            )

        keyboard = build_trader_actions_keyboard(address, "hyperliquid", is_tracked)

        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Error refreshing analytics: {e}")
        await callback.answer(f"Error: {str(e)[:50]}", show_alert=True)


@router.callback_query(TraderAction.filter(F.action == "track"))
async def callback_track_wallet(
    callback: CallbackQuery,
    callback_data: TraderAction,
    wallet_service=None,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Track a wallet for alerts via WalletService."""
    if not wallet_service:
        await callback.answer("Wallet service not available", show_alert=True)
        return

    address = callback_data.address
    user_id = callback.from_user.id
    exchange = callback_data.exchange

    result = await wallet_service.track_wallet(
        user_id=user_id,
        address=address,
        exchange=exchange,
    )

    if result.success:
        if redis_client:
            await redis_client.set(f"addr_map:{address[:20]}", address, ex=86400 * 30)
        await callback.answer(f"Now tracking {address[:10]}...", show_alert=True)
        try:
            keyboard = build_trader_actions_keyboard(address, exchange, is_tracked=True)
            await callback.message.edit_reply_markup(reply_markup=keyboard)
        except Exception:
            pass
    else:
        await callback.answer(result.error or "Failed to track", show_alert=True)


@router.callback_query(TraderAction.filter(F.action == "untrack"))
async def callback_untrack_wallet(
    callback: CallbackQuery,
    callback_data: TraderAction,
    wallet_service=None,
) -> None:
    """Stop tracking a wallet via WalletService."""
    if not wallet_service:
        await callback.answer("Wallet service not available", show_alert=True)
        return

    address = callback_data.address
    user_id = callback.from_user.id
    exchange = callback_data.exchange

    result = await wallet_service.untrack_wallet(user_id, address)

    if result.success:
        await callback.answer(f"Stopped tracking {address[:10]}...", show_alert=True)
        try:
            keyboard = build_trader_actions_keyboard(address, exchange, is_tracked=False)
            await callback.message.edit_reply_markup(reply_markup=keyboard)
        except Exception:
            pass
    else:
        await callback.answer(result.error or "Failed to untrack", show_alert=True)


@router.callback_query(TraderAction.filter(F.action == "positions"))
async def callback_show_positions(
    callback: CallbackQuery,
    callback_data: TraderAction,
    hyperliquid_client: Optional[HyperliquidClient] = None,
) -> None:
    """Show positions for the trader."""
    await callback.answer("Loading positions...")

    if not hyperliquid_client:
        await callback.answer("Client not available", show_alert=True)
        return

    address = callback_data.address

    try:
        state = await hyperliquid_client.get_user_state(address)

        if not state.positions:
            await callback.answer("No open positions", show_alert=True)
            return

        lines = [f"<b>Positions for {shorten_address(address)}</b>\n"]

        for pos in state.positions[:5]:
            dir_emoji = "🟢" if pos.direction == Direction.LONG else "🔴"
            dir_text = "LONG" if pos.direction == Direction.LONG else "SHORT"
            roe_pct = float(pos.return_on_equity) * 100
            pnl_str = format_pnl(pos.unrealized_pnl, roe_pct)

            lines.append(
                f"{dir_emoji} <b>{pos.coin}</b> {dir_text}\n"
                f"   ${float(pos.position_value):,.0f} | {pnl_str}"
            )

        if len(state.positions) > 5:
            lines.append(f"\n<i>+ {len(state.positions) - 5} more</i>")

        await callback.message.answer("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error getting positions: {e}")
        await callback.answer(f"Error: {str(e)[:50]}", show_alert=True)


@router.callback_query(TraderAction.filter(F.action == "pnl"))
async def callback_show_pnl(
    callback: CallbackQuery,
    callback_data: TraderAction,
    hyperliquid_client: Optional[HyperliquidClient] = None,
) -> None:
    """Show PnL history for the trader."""
    await callback.answer("Loading PnL...")

    if not hyperliquid_client:
        await callback.answer("Client not available", show_alert=True)
        return

    address = callback_data.address

    try:
        fills = await hyperliquid_client.get_user_fills(address, limit=20)

        if not fills:
            await callback.answer("No recent trades", show_alert=True)
            return

        lines = [f"<b>Recent PnL for {shorten_address(address)}</b>\n"]

        for fill in fills[:10]:
            if fill.closed_pnl == 0:
                continue

            pnl_emoji = "🟢" if fill.closed_pnl > 0 else "🔴"
            time_str = fill.datetime.strftime("%m/%d %H:%M")

            lines.append(
                f"{pnl_emoji} <b>{fill.coin}</b> {format_usd(fill.closed_pnl, sign=True)}\n"
                f"   {time_str}"
            )

        total = sum(float(f.closed_pnl) for f in fills)
        total_emoji = "🟢" if total >= 0 else "🔴"
        lines.append(f"\n{total_emoji} <b>Total:</b> {format_usd(total, sign=True)}")

        await callback.message.answer("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error getting PnL: {e}")
        await callback.answer(f"Error: {str(e)[:50]}", show_alert=True)


@router.callback_query(CopyTradingAction.filter(F.action == "enable"))
async def callback_enable_copy(
    callback: CallbackQuery,
    callback_data: CopyTradingAction,
    copy_trading_service=None,
) -> None:
    """Enable copy trading signals for a trader."""
    if not copy_trading_service:
        await callback.answer("Copy trading not available.", show_alert=True)
        return

    address = callback_data.address
    user_id = callback.from_user.id

    try:
        await copy_trading_service.enable_copy_trading(
            user_id=user_id,
            address=address,
            mode="signals",
        )
        await callback.answer(
            f"Copy trading signals enabled for {address[:10]}...\n"
            "You'll receive alerts when this trader opens/closes positions.",
            show_alert=True,
        )
    except Exception as e:
        await callback.answer(f"Error: {str(e)[:50]}", show_alert=True)


@router.callback_query(WatchlistAction.filter(F.action == "add"))
async def callback_add_to_watchlist(
    callback: CallbackQuery,
    callback_data: WatchlistAction,
    wallet_service=None,
) -> None:
    """Add trader to watchlist (track wallet)."""
    if not wallet_service:
        await callback.answer("Wallet service not available.", show_alert=True)
        return

    result = await wallet_service.track_wallet(
        user_id=callback.from_user.id,
        address=callback_data.address,
        label="watchlist",
    )
    if result.success:
        await callback.answer(f"Added {callback_data.address[:10]}... to watchlist", show_alert=True)
    else:
        await callback.answer(result.error or "Failed", show_alert=True)


@router.message(Command("copy_status"))
async def cmd_copy_status(
    message: Message,
    command: CommandObject,
    copy_trading_service=None,
) -> None:
    """Show active copy trading configurations."""
    if not copy_trading_service:
        await message.reply("Copy trading service not available.")
        return

    configs = await copy_trading_service.get_user_copy_wallets(message.from_user.id)

    if not configs:
        await message.reply("No active copy trading configurations.\n\nUse the 'Enable Copy' button on /hl_analytics to start.")
        return

    lines = ["<b>Active Copy Trading</b>\n"]
    for cfg in configs:
        addr_short = f"{cfg.address[:6]}...{cfg.address[-4:]}"
        lines.append(
            f"<code>{addr_short}</code> — {cfg.mode}\n"
            f"  Min: ${cfg.min_trade_size:,.0f}"
        )

    await message.reply("\n".join(lines), parse_mode="HTML")
