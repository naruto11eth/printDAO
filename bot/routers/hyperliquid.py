"""Hyperliquid commands router."""

import logging
from decimal import Decimal
from typing import Optional

from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command, CommandObject

from shared.utils import shorten_address, is_valid_ethereum_address, escape_html
from indexers.hyperliquid import HyperliquidClient, Direction
from bot.services.position_indexer import PositionIndexer

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


@router.message(Command("hl_trades"))
async def cmd_hl_trades(
    message: Message,
    command: CommandObject,
    hyperliquid_client: Optional[HyperliquidClient] = None,
) -> None:
    """Show recent trades for a coin.

    Usage: /hl_trades <coin> [limit]
    """
    if not command.args:
        await message.reply(
            "<b>Usage:</b> /hl_trades &lt;coin&gt; [limit]\n\n"
            "<b>Examples:</b>\n"
            "<code>/hl_trades BTC</code>\n"
            "<code>/hl_trades ETH 20</code>",
            parse_mode="HTML",
        )
        return

    if not hyperliquid_client:
        await message.reply("Hyperliquid client is not available.")
        return

    parts = command.args.strip().upper().split()
    coin = parts[0]
    limit = min(int(parts[1]), 50) if len(parts) > 1 else 10

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        trades = await hyperliquid_client.get_recent_trades(coin, limit)

        if not trades:
            await message.reply(
                f"No recent trades found for <b>{coin}</b>",
                parse_mode="HTML",
            )
            return

        lines = [
            f"<b>Recent {coin} Trades</b>",
            f"<i>Last {len(trades)} trades</i>\n",
        ]

        for trade in trades[:limit]:
            dir_emoji = "🟢" if trade.direction == Direction.LONG else "🔴"
            dir_text = "LONG" if trade.direction == Direction.LONG else "SHORT"
            time_str = trade.datetime.strftime("%H:%M:%S")
            notional = format_usd_short(trade.notional_usd)

            lines.append(
                f"{dir_emoji} <b>{dir_text}</b> {notional}\n"
                f"   {float(trade.size):,.4f} @ ${float(trade.price):,.2f}\n"
                f"   {time_str} | <code>{shorten_address(trade.users[0])}</code>"
            )

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
            pnl_str = format_usd(pos.unrealized_pnl)
            roe_str = format_percent(float(pos.return_on_equity) * 100)

            leverage_str = f" {float(pos.leverage):.0f}x" if pos.leverage else ""

            lines.append(
                f"\n{dir_emoji} <b>{pos.coin}</b> {dir_text}{leverage_str}\n"
                f"   Size: {float(pos.abs_size):,.4f} (${float(pos.position_value):,.0f})\n"
                f"   Entry: ${float(pos.entry_px):,.2f}\n"
                f"   PnL: {pnl_str} ({roe_str})"
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
    """Show recent whale trades on Hyperliquid.

    Usage: /hl_whales [min_size_millions]
    Default min_size: $10M
    """
    min_size = Decimal("10000000")  # $10M default

    if command.args:
        try:
            val = float(command.args.strip().replace("$", "").replace(",", "").replace("M", "").replace("m", ""))
            # Assume input is in millions if small
            if val < 1000:
                val *= 1_000_000
            min_size = Decimal(str(val))
        except ValueError:
            await message.reply("Invalid amount. Use a number like 10 (for $10M) or 5000000")
            return

    if not hyperliquid_client:
        await message.reply("Hyperliquid client is not available.")
        return

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        trades = await hyperliquid_client.get_whale_trades(threshold=min_size, limit=10)

        if not trades:
            await message.reply(
                f"No whale trades found above {format_usd_short(min_size)}",
                parse_mode="HTML",
            )
            return

        lines = [
            f"<b>🐋 Hyperliquid Whale Trades</b>",
            f"<i>Trades above {format_usd_short(min_size)}</i>\n",
        ]

        for trade in trades:
            dir_emoji = "🟢" if trade.direction == Direction.LONG else "🔴"
            dir_text = "LONG" if trade.direction == Direction.LONG else "SHORT"
            time_str = trade.datetime.strftime("%m/%d %H:%M")
            trader = shorten_address(trade.users[0])

            lines.append(
                f"{dir_emoji} <b>{format_usd_short(trade.notional_usd)}</b> {trade.coin} {dir_text}\n"
                f"   {float(trade.size):,.4f} @ ${float(trade.price):,.2f}\n"
                f"   {time_str} | <code>{trader}</code>"
            )

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
