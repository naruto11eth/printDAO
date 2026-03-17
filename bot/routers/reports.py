"""Scheduled reports commands router."""

import logging
from typing import Optional

from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command, CommandObject

from bot.services.report_service import ReportService, VALID_FREQUENCIES

logger = logging.getLogger(__name__)

router = Router(name="reports")


@router.message(Command("subscribe_report"))
async def cmd_subscribe_report(
    message: Message,
    command: CommandObject,
    report_service: Optional[ReportService] = None,
) -> None:
    """Subscribe to scheduled reports.

    Usage: /subscribe_report <frequency>
    Frequencies: 4h, 8h, daily, weekly
    """
    if not report_service:
        await message.reply("Report service not available.")
        return

    if not command.args:
        freqs = ", ".join(sorted(VALID_FREQUENCIES))
        await message.reply(
            f"<b>Usage:</b> /subscribe_report &lt;frequency&gt;\n\n"
            f"<b>Frequencies:</b> {freqs}\n\n"
            f"<b>Example:</b> <code>/subscribe_report daily</code>",
            parse_mode="HTML",
        )
        return

    frequency = command.args.strip().lower()
    result = await report_service.subscribe(message.from_user.id, frequency)

    if isinstance(result, str):
        await message.reply(f"Error: {result}")
    else:
        await message.reply(
            f"Subscribed to <b>{frequency}</b> reports.\n\n"
            f"Use /unsubscribe_report to stop.",
            parse_mode="HTML",
        )


@router.message(Command("unsubscribe_report"))
async def cmd_unsubscribe_report(
    message: Message,
    report_service: Optional[ReportService] = None,
) -> None:
    """Unsubscribe from scheduled reports."""
    if not report_service:
        await message.reply("Report service not available.")
        return

    removed = await report_service.unsubscribe(message.from_user.id)
    if removed:
        await message.reply("Unsubscribed from reports.")
    else:
        await message.reply("No active report subscription found.")


@router.message(Command("set_timezone"))
async def cmd_set_timezone(
    message: Message,
    command: CommandObject,
    report_service: Optional[ReportService] = None,
) -> None:
    """Set timezone for scheduled reports.

    Usage: /set_timezone <tz>
    Example: /set_timezone US/Pacific
    """
    if not report_service:
        await message.reply("Report service not available.")
        return

    if not command.args:
        await message.reply(
            "<b>Usage:</b> /set_timezone &lt;timezone&gt;\n\n"
            "<b>Examples:</b> UTC, US/Pacific, US/Eastern, Europe/London",
            parse_mode="HTML",
        )
        return

    tz = command.args.strip()
    sub = await report_service.get_subscription(message.from_user.id)
    if not sub:
        await message.reply("Subscribe to reports first: /subscribe_report daily")
        return

    result = await report_service.subscribe(message.from_user.id, sub.frequency, timezone=tz)
    if isinstance(result, str):
        await message.reply(f"Error: {result}")
    else:
        await message.reply(f"Timezone set to <b>{tz}</b>", parse_mode="HTML")


@router.message(Command("report_preview"))
async def cmd_report_preview(
    message: Message,
    report_service: Optional[ReportService] = None,
) -> None:
    """Preview what a report looks like."""
    if not report_service:
        await message.reply("Report service not available.")
        return

    await message.bot.send_chat_action(message.chat.id, "typing")
    digest = await report_service.generate_digest(message.from_user.id, period_seconds=86400)
    await message.reply(digest, parse_mode="HTML")
