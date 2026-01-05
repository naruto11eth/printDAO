"""Hyperliquid commands router."""

import logging
from decimal import Decimal
from typing import Optional, List, Dict, Any

from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command, CommandObject

from shared.utils import shorten_address, is_valid_ethereum_address, escape_html
from indexers.hyperliquid import HyperliquidClient, Direction
from bot.services.position_indexer import PositionIndexer
from bot.utils.filters import parse_filter_args, format_filter_summary
from bot.storage.redis_client import RedisClient

logger = logging.getLogger(__name__)

router = Router(name="hyperliquid")


def format_usd(value: Decimal | float) -> str:
    """Format USD value with sign."""
    val = float(value) if isinstance(value, Decimal) else value
    if val >= 0:
        return f"+${val:,.2f}"
    return f"-${abs(val):,.2f}"


def format_usd_short(value: Decimal | float) -> str:
    """Format USD value in short form (K/M)."""
    val = float(value) if isinstance(value, Decimal) else value
    if abs(val) >= 1_000_000:
        return f"${val / 1_000_000:,.2f}M"
    elif abs(val) >= 1_000:
        return f"${val / 1_000:,.1f}K"
    return f"${val:,.2f}"


def format_percent(value: Decimal | float) -> str:
    """Format percentage with sign."""
    val = float(value) if isinstance(value, Decimal) else value
    if val >= 0:
        return f"+{val:.2f}%"
    return f"{val:.2f}%"


def format_pnl(usd_value: Decimal | float, percent_value: Decimal | float) -> str:
    """Format PNL as USD with percentage in parentheses."""
    usd = float(usd_value) if isinstance(usd_value, Decimal) else usd_value
    pct = float(percent_value) if isinstance(percent_value, Decimal) else percent_value
    usd_str = f"+${usd:,.2f}" if usd >= 0 else f"-${abs(usd):,.2f}"
    pct_str = f"+{pct:.1f}%" if pct >= 0 else f"{pct:.1f}%"
    return f"{usd_str} ({pct_str})"


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
            time_str = trade.datetime.strftime("%H:%M:%S")
            notional = format_usd_short(trade.notional_usd)

            lines.append(
                f"{dir_emoji} <b>{dir_text}</b> {notional}\n"
                f"   {float(trade.size):,.4f} @ ${float(trade.price):,.2f}\n"
                f"   {time_str} | <code>{shorten_address(trade.users[0])}</code>"
            )

        if len(trades) > 15:
            lines.append(f"\n<i>... and {len(trades) - 15} more</i>")

        await message.reply("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error getting trades for {coin}: {e}")
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
            f"{pnl_emoji} <b>Total PnL:</b> {format_usd(total_pnl)}\n",
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
            f"{pnl_emoji} <b>Total PnL:</b> {format_usd(total_pnl)}",
            f"<b>Realized:</b> {format_usd(realized_pnl)}",
            f"<b>Unrealized:</b> {format_usd(unrealized_pnl)}\n",
            f"<b>Recent Trades:</b> {len(fills)}",
            f"<b>Win Rate:</b> {win_rate:.1f}%",
            f"<b>W/L:</b> {len(winning)}W / {len(losing)}L",
        ]

        if fills:
            # Show largest win/loss
            best = max(fills, key=lambda f: f.closed_pnl)
            worst = min(fills, key=lambda f: f.closed_pnl)

            if best.closed_pnl > 0:
                lines.append(f"\n🏆 <b>Best:</b> {best.coin} {format_usd(best.closed_pnl)}")
            if worst.closed_pnl < 0:
                lines.append(f"💀 <b>Worst:</b> {worst.coin} {format_usd(worst.closed_pnl)}")

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
            pnl_str = f" | PnL: {format_usd(fill.closed_pnl)}" if fill.closed_pnl != 0 else ""

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
            time_str = trade.datetime.strftime("%m/%d %H:%M")
            trader = shorten_address(trade.users[0])

            lines.append(
                f"{dir_emoji} <b>{format_usd_short(trade.notional_usd)}</b> {trade.coin} {dir_text}\n"
                f"   {float(trade.size):,.4f} @ ${float(trade.price):,.2f}\n"
                f"   {time_str} | <code>{trader}</code>"
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
    position_indexer: Optional[PositionIndexer] = None,
) -> None:
    """Show TrySuper-style trader analytics.

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
        # Get metrics from position indexer if available, otherwise compute directly
        if position_indexer:
            metrics = await position_indexer.get_or_compute_metrics(address, "hyperliquid")
        else:
            # Fallback: compute basic metrics directly
            state = await hyperliquid_client.get_user_state(address)
            fills = await hyperliquid_client.get_user_fills(address, limit=200)

            winning = [f for f in fills if f.closed_pnl > 0]
            losing = [f for f in fills if f.closed_pnl < 0]
            win_rate = (len(winning) / len(fills) * 100) if fills else 0

            total_profit = sum(float(f.closed_pnl) for f in winning)
            total_loss = abs(sum(float(f.closed_pnl) for f in losing))
            profit_factor = (total_profit / total_loss) if total_loss > 0 else 0

            longs = [f for f in fills if "Long" in f.direction]
            shorts = [f for f in fills if "Short" in f.direction]

            if len(longs) > len(shorts) * 1.5:
                direction_bias = "Long Biased"
            elif len(shorts) > len(longs) * 1.5:
                direction_bias = "Short Biased"
            else:
                direction_bias = "Neutral"

            # Trades per day
            if fills:
                import time
                oldest = min(fills, key=lambda f: f.time_ms)
                days = max(1, (time.time() * 1000 - oldest.time_ms) / (1000 * 86400))
                trades_per_day = len(fills) / days
            else:
                trades_per_day = 0

            if trades_per_day > 10:
                trader_type = "Scalper"
            elif trades_per_day > 2:
                trader_type = "Day Trader"
            elif trades_per_day > 0.5:
                trader_type = "Swing Trader"
            else:
                trader_type = "Position Trader"

            realized_pnl = sum(float(f.closed_pnl) for f in fills)

            # Build metrics dict for display
            metrics = type('Metrics', (), {
                'account_value': float(state.account_value),
                'all_time_pnl': realized_pnl + float(state.total_pnl),
                'trader_type': trader_type,
                'account_age_days': 0,
                'profit_factor': min(profit_factor, 99.99),
                'win_rate': win_rate,
                'winning_trades': len(winning),
                'losing_trades': len(losing),
                'total_trades': len(fills),
                'trades_per_day': trades_per_day,
                'direction_bias': direction_bias,
                'max_drawdown': 0,
                'day_7_roe': None,
                'day_30_roe': None,
                'day_90_roe': None,
            })()

        if not metrics:
            await message.reply(
                f"Could not compute metrics for <code>{shorten_address(address)}</code>",
                parse_mode="HTML",
            )
            return

        # Format the analytics display (TrySuper style)
        pnl_emoji = "🟢" if metrics.all_time_pnl >= 0 else "🔴"

        lines = [
            f"<b>📊 Trader Analytics</b>",
            f"<code>{shorten_address(address)}</code>\n",
            f"<b>Account Value:</b> ${metrics.account_value:,.2f}",
            f"{pnl_emoji} <b>All-Time PnL:</b> {format_usd(metrics.all_time_pnl)}\n",
            f"<b>━━━ Trader Profile ━━━</b>",
            f"<b>Type:</b> {metrics.trader_type}",
        ]

        if metrics.account_age_days > 0:
            lines.append(f"<b>Account Age:</b> {metrics.account_age_days} days")

        lines.extend([
            f"<b>Direction:</b> {metrics.direction_bias}",
            f"<b>Trades/Day:</b> {metrics.trades_per_day:.1f}\n",
            f"<b>━━━ Performance ━━━</b>",
            f"<b>Profit Factor:</b> {metrics.profit_factor:.2f}",
            f"<b>Win Rate:</b> {metrics.win_rate:.1f}%",
            f"<b>W/L:</b> {metrics.winning_trades}W / {metrics.losing_trades}L",
            f"<b>Total Trades:</b> {metrics.total_trades}",
        ])

        if metrics.max_drawdown > 0:
            lines.append(f"<b>Max Drawdown:</b> -{metrics.max_drawdown:.1f}%")

        # ROE section
        roe_lines = []
        if metrics.day_7_roe is not None:
            roe_lines.append(f"7d: {format_percent(metrics.day_7_roe)}")
        if metrics.day_30_roe is not None:
            roe_lines.append(f"30d: {format_percent(metrics.day_30_roe)}")
        if metrics.day_90_roe is not None:
            roe_lines.append(f"90d: {format_percent(metrics.day_90_roe)}")

        if roe_lines:
            lines.append(f"\n<b>━━━ ROE ━━━</b>")
            lines.append(" | ".join(roe_lines))

        await message.reply("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error getting analytics for {address}: {e}")
        await message.reply(f"Error fetching analytics: {str(e)}")
