"""Leaderboard commands router."""

import logging
from typing import Optional

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command, CommandObject

from bot.services.leaderboard_service import LeaderboardService, format_leaderboard_page
from bot.services.analytics_service import AnalyticsService, format_trader_analytics
from bot.keyboards.leaderboard import LeaderboardCB, TraderDetailCB, build_leaderboard_keyboard
from bot.keyboards.analytics import build_trader_actions_keyboard
from bot.storage.redis_client import RedisClient
from shared.utils import is_valid_ethereum_address, shorten_address

logger = logging.getLogger(__name__)

router = Router(name="leaderboard")


@router.message(Command("leaderboard"))
async def cmd_leaderboard(
    message: Message,
    command: CommandObject,
    leaderboard_service: Optional[LeaderboardService] = None,
) -> None:
    """Show top traders leaderboard.

    Usage: /leaderboard [page]
    """
    if not leaderboard_service:
        await message.reply("Leaderboard service not available.")
        return

    page = 0
    if command.args:
        try:
            page = max(0, int(command.args.strip()) - 1)
        except ValueError:
            pass

    await message.bot.send_chat_action(message.chat.id, "typing")

    page_size = 10
    total = await leaderboard_service.get_leaderboard_size()
    entries = await leaderboard_service.get_leaderboard_page(page=page, page_size=page_size)

    text = format_leaderboard_page(entries, page, total, page_size)
    total_pages = max(1, (total + page_size - 1) // page_size)
    keyboard = build_leaderboard_keyboard(page, total_pages, entries)

    await message.reply(text, parse_mode="HTML", reply_markup=keyboard.as_markup())


@router.message(Command("rank"))
async def cmd_rank(
    message: Message,
    command: CommandObject,
    leaderboard_service: Optional[LeaderboardService] = None,
) -> None:
    """Show trader rank across metrics.

    Usage: /rank <address>
    """
    if not command.args:
        await message.reply("<b>Usage:</b> /rank &lt;address&gt;", parse_mode="HTML")
        return

    if not leaderboard_service:
        await message.reply("Leaderboard service not available.")
        return

    address = command.args.strip()
    if not is_valid_ethereum_address(address):
        await message.reply("Invalid Ethereum address.")
        return

    rank = await leaderboard_service.get_trader_rank(address)
    total = await leaderboard_service.get_leaderboard_size()

    if rank is None:
        await message.reply(
            f"<code>{shorten_address(address)}</code> is not on the leaderboard.\n\n"
            f"Use /hl_analytics to compute their metrics first.",
            parse_mode="HTML",
        )
        return

    trader_key = f"trader:{address.lower()}"
    data = await leaderboard_service.redis.hgetall(trader_key)
    score = float(data.get("composite_score", 0))
    pnl = float(data.get("all_time_pnl", 0))

    await message.reply(
        f"<b>Rank: #{rank}</b> of {total}\n\n"
        f"<code>{shorten_address(address)}</code>\n"
        f"Score: <b>{score:.1f}</b>/100\n"
        f"PnL: ${pnl:,.2f}",
        parse_mode="HTML",
    )


# --- Callback Handlers ---

@router.callback_query(LeaderboardCB.filter(F.action.in_({"p", "n"})))
async def callback_leaderboard_paginate(
    callback: CallbackQuery,
    callback_data: LeaderboardCB,
    leaderboard_service: Optional[LeaderboardService] = None,
) -> None:
    """Handle leaderboard pagination."""
    await callback.answer()

    if not leaderboard_service:
        return

    page = callback_data.page
    page_size = 10
    total = await leaderboard_service.get_leaderboard_size()
    entries = await leaderboard_service.get_leaderboard_page(page=page, page_size=page_size)

    text = format_leaderboard_page(entries, page, total, page_size)
    total_pages = max(1, (total + page_size - 1) // page_size)
    keyboard = build_leaderboard_keyboard(page, total_pages, entries)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard.as_markup())


@router.callback_query(TraderDetailCB.filter(F.action == "a"))
async def callback_trader_detail(
    callback: CallbackQuery,
    callback_data: TraderDetailCB,
    redis_client: Optional[RedisClient] = None,
    analytics_service: Optional[AnalyticsService] = None,
    leaderboard_service: Optional[LeaderboardService] = None,
) -> None:
    """Show detailed analytics for a trader from leaderboard."""
    await callback.answer("Loading analytics...")

    addr_prefix = callback_data.addr_prefix
    if not addr_prefix or not redis_client:
        return

    full_addr = await redis_client.get(f"addr_map:{addr_prefix}")
    address = full_addr if full_addr else addr_prefix

    if not analytics_service:
        return

    try:
        analytics = await analytics_service.get_trader_analytics(address, "hyperliquid")
        text = format_trader_analytics(analytics)
        keyboard = build_trader_actions_keyboard(address, "hyperliquid")
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Error loading trader detail: {e}")
        await callback.answer(f"Error: {str(e)[:50]}", show_alert=True)
