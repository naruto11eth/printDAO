"""User settings and preferences router."""

import logging
import time
from typing import Optional

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command, CommandObject
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.filters.callback_data import CallbackData

from bot.storage.redis_client import RedisClient
from bot.storage.models import (
    UserPreferences,
    WhaleDetectionConfig,
    NotificationPreference,
)
from shared.constants import EXCHANGES, INTERVAL_LIMITS

logger = logging.getLogger(__name__)

router = Router(name="settings")


class SettingsAction(CallbackData, prefix="settings"):
    """Callback data for settings actions."""

    action: str  # "whale_min", "whale_new", "whale_mult", "interval", "notify"
    value: str = ""


async def get_user_preferences(
    redis: RedisClient,
    user_id: int,
) -> UserPreferences:
    """Get or create user preferences."""
    key = f"user:{user_id}:preferences"
    data = await redis.get(key)

    if data:
        return UserPreferences.from_dict(data)

    prefs = UserPreferences(user_id=user_id)
    await redis.set(key, prefs.to_dict())
    return prefs


async def save_user_preferences(
    redis: RedisClient,
    prefs: UserPreferences,
) -> None:
    """Save user preferences."""
    prefs.updated_at = time.time()
    key = f"user:{prefs.user_id}:preferences"
    await redis.set(key, prefs.to_dict())


def build_whale_config_keyboard(config: WhaleDetectionConfig) -> InlineKeyboardBuilder:
    """Build keyboard for whale config editing."""
    builder = InlineKeyboardBuilder()

    builder.button(
        text=f"Min Size: ${config.min_trade_size_usd:,.0f}",
        callback_data=SettingsAction(action="whale_min_edit").pack(),
    )
    builder.button(
        text=f"New Whale: ${config.new_wallet_first_trade_threshold:,.0f}",
        callback_data=SettingsAction(action="whale_new_edit").pack(),
    )
    builder.button(
        text=f"Unusual: {config.unusual_size_multiplier}x",
        callback_data=SettingsAction(action="whale_mult_edit").pack(),
    )

    # Toggle buttons
    new_whale_emoji = "✅" if config.alert_on_new_whales else "❌"
    unusual_emoji = "✅" if config.alert_on_unusual_activity else "❌"
    accum_emoji = "✅" if config.alert_on_accumulation else "❌"

    builder.button(
        text=f"{new_whale_emoji} New Whales",
        callback_data=SettingsAction(action="toggle_new_whales").pack(),
    )
    builder.button(
        text=f"{unusual_emoji} Unusual Activity",
        callback_data=SettingsAction(action="toggle_unusual").pack(),
    )
    builder.button(
        text=f"{accum_emoji} Accumulation",
        callback_data=SettingsAction(action="toggle_accum").pack(),
    )

    builder.button(
        text="🔄 Reset to Defaults",
        callback_data=SettingsAction(action="whale_reset").pack(),
    )

    builder.adjust(2, 1, 3, 1)
    return builder


def build_intervals_keyboard(intervals: dict) -> InlineKeyboardBuilder:
    """Build keyboard for interval editing."""
    builder = InlineKeyboardBuilder()

    for exchange, seconds in intervals.items():
        if seconds < 60:
            time_str = f"{seconds}s"
        else:
            time_str = f"{seconds // 60}m"

        builder.button(
            text=f"{exchange.title()}: {time_str}",
            callback_data=SettingsAction(action="interval_edit", value=exchange).pack(),
        )

    builder.button(
        text="🔄 Reset to Defaults",
        callback_data=SettingsAction(action="interval_reset").pack(),
    )

    builder.adjust(2, 1)
    return builder


@router.message(Command("settings"))
async def cmd_settings(
    message: Message,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Show all user settings."""
    if not redis_client:
        await message.reply("Settings service unavailable.")
        return

    prefs = await get_user_preferences(redis_client, message.from_user.id)

    lines = [
        "<b>Your Settings</b>\n",
        f"<b>Notifications:</b> {prefs.notification_preference.value.title()}",
        f"<b>Copy Trading:</b> {'Enabled' if prefs.copy_trading_enabled else 'Disabled'}",
    ]

    # Intervals
    lines.append("\n<b>Monitoring Intervals:</b>")
    for exchange, seconds in prefs.monitoring_intervals.items():
        if seconds < 60:
            time_str = f"{seconds}s"
        else:
            time_str = f"{seconds // 60}m"
        lines.append(f"  {exchange.title()}: {time_str}")

    # Whale config summary
    wc = prefs.whale_config
    lines.extend([
        "\n<b>Whale Detection:</b>",
        f"  Min Trade: ${wc.min_trade_size_usd:,.0f}",
        f"  New Whale Threshold: ${wc.new_wallet_first_trade_threshold:,.0f}",
    ])

    lines.append(
        "\n<i>Commands: /whale_config, /setinterval, /notify</i>"
    )

    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("whale_config"))
async def cmd_whale_config(
    message: Message,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Show/edit whale detection configuration."""
    if not redis_client:
        await message.reply("Settings service unavailable.")
        return

    prefs = await get_user_preferences(redis_client, message.from_user.id)
    config = prefs.whale_config

    keyboard = build_whale_config_keyboard(config)

    lines = [
        "<b>Whale Detection Settings</b>\n",
        f"<b>Min Trade Size:</b> ${config.min_trade_size_usd:,.0f}",
        f"<b>New Wallet Threshold:</b> ${config.new_wallet_first_trade_threshold:,.0f}",
        f"<b>New Wallet Age:</b> {config.new_wallet_age_days} days",
        f"<b>Unusual Size Multiplier:</b> {config.unusual_size_multiplier}x\n",
        "<b>Alert Types:</b>",
        f"  {'✅' if config.alert_on_new_whales else '❌'} New Whales",
        f"  {'✅' if config.alert_on_unusual_activity else '❌'} Unusual Activity",
        f"  {'✅' if config.alert_on_accumulation else '❌'} Accumulation",
    ]

    await message.reply(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=keyboard.as_markup(),
    )


@router.message(Command("setinterval"))
async def cmd_setinterval(
    message: Message,
    command: CommandObject,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Set monitoring interval for an exchange.

    Usage: /setinterval <exchange> <seconds>
    """
    if not redis_client:
        await message.reply("Settings service unavailable.")
        return

    if not command.args:
        prefs = await get_user_preferences(redis_client, message.from_user.id)
        keyboard = build_intervals_keyboard(prefs.monitoring_intervals)

        lines = [
            "<b>Usage:</b> /setinterval &lt;exchange&gt; &lt;seconds&gt;\n",
            "<b>Current Intervals:</b>",
        ]

        for exchange, seconds in prefs.monitoring_intervals.items():
            limits = INTERVAL_LIMITS.get(exchange, {})
            lines.append(
                f"  {exchange.title()}: {seconds}s "
                f"(min: {limits.get('min', 30)}s, max: {limits.get('max', 3600)}s)"
            )

        lines.append("\n<b>Examples:</b>")
        lines.append("<code>/setinterval hyperliquid 60</code>")
        lines.append("<code>/setinterval polymarket 300</code>")

        await message.reply(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=keyboard.as_markup(),
        )
        return

    parts = command.args.split()
    if len(parts) < 2:
        await message.reply("Please provide exchange and interval in seconds.")
        return

    exchange = parts[0].lower()
    try:
        interval = int(parts[1])
    except ValueError:
        await message.reply("Invalid interval. Please provide a number in seconds.")
        return

    if exchange not in INTERVAL_LIMITS:
        exchanges = ", ".join(INTERVAL_LIMITS.keys())
        await message.reply(f"Invalid exchange. Available: {exchanges}")
        return

    limits = INTERVAL_LIMITS[exchange]
    interval = max(limits["min"], min(limits["max"], interval))

    prefs = await get_user_preferences(redis_client, message.from_user.id)
    prefs.monitoring_intervals[exchange] = interval
    await save_user_preferences(redis_client, prefs)

    if interval < 60:
        time_str = f"{interval} seconds"
    else:
        time_str = f"{interval // 60} minute(s)"

    await message.reply(
        f"✅ Monitoring interval for <b>{exchange.title()}</b> set to {time_str}",
        parse_mode="HTML",
    )


@router.message(Command("intervals"))
async def cmd_intervals(
    message: Message,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Show current monitoring intervals."""
    if not redis_client:
        await message.reply("Settings service unavailable.")
        return

    prefs = await get_user_preferences(redis_client, message.from_user.id)
    keyboard = build_intervals_keyboard(prefs.monitoring_intervals)

    lines = ["<b>Monitoring Intervals:</b>\n"]

    for exchange, seconds in prefs.monitoring_intervals.items():
        if seconds < 60:
            time_str = f"{seconds} seconds"
        else:
            time_str = f"{seconds // 60} minute(s)"

        limits = INTERVAL_LIMITS.get(exchange, {})
        lines.append(
            f"<b>{exchange.title()}:</b> {time_str}\n"
            f"  Range: {limits.get('min', 30)}s - {limits.get('max', 3600) // 60}m"
        )

    lines.append("\n<i>Use /setinterval &lt;exchange&gt; &lt;seconds&gt; to change</i>")

    await message.reply(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=keyboard.as_markup(),
    )


@router.message(Command("notify"))
async def cmd_notify(
    message: Message,
    command: CommandObject,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Set notification preferences.

    Usage: /notify <all|whale_only|summary|none>
    """
    if not redis_client:
        await message.reply("Settings service unavailable.")
        return

    prefs = await get_user_preferences(redis_client, message.from_user.id)

    if not command.args:
        builder = InlineKeyboardBuilder()

        for pref in NotificationPreference:
            emoji = "✅" if prefs.notification_preference == pref else "⬜"
            builder.button(
                text=f"{emoji} {pref.value.replace('_', ' ').title()}",
                callback_data=SettingsAction(action="notify", value=pref.value).pack(),
            )

        builder.adjust(2)

        lines = [
            "<b>Notification Preferences</b>\n",
            f"<b>Current:</b> {prefs.notification_preference.value.replace('_', ' ').title()}\n",
            "<b>Options:</b>",
            "  <b>All</b> - All whale activity",
            "  <b>Whale Only</b> - Only tracked wallets",
            "  <b>Summary</b> - Daily summaries only",
            "  <b>None</b> - No notifications",
        ]

        await message.reply(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=builder.as_markup(),
        )
        return

    pref_value = command.args.strip().lower()

    try:
        new_pref = NotificationPreference(pref_value)
    except ValueError:
        valid = ", ".join(p.value for p in NotificationPreference)
        await message.reply(f"Invalid preference. Valid: {valid}")
        return

    prefs.notification_preference = new_pref
    await save_user_preferences(redis_client, prefs)

    await message.reply(
        f"✅ Notifications set to <b>{new_pref.value.replace('_', ' ').title()}</b>",
        parse_mode="HTML",
    )


@router.message(Command("mute"))
async def cmd_mute(
    message: Message,
    command: CommandObject,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Mute notifications for a duration.

    Usage: /mute <minutes>
    """
    if not redis_client:
        await message.reply("Settings service unavailable.")
        return

    if not command.args:
        await message.reply(
            "<b>Usage:</b> /mute &lt;minutes&gt;\n\n"
            "<b>Examples:</b>\n"
            "<code>/mute 30</code> - Mute for 30 minutes\n"
            "<code>/mute 60</code> - Mute for 1 hour\n\n"
            "Use /unmute to turn notifications back on.",
            parse_mode="HTML",
        )
        return

    try:
        minutes = int(command.args.strip())
    except ValueError:
        await message.reply("Invalid duration. Please provide number of minutes.")
        return

    if minutes < 1 or minutes > 1440:
        await message.reply("Duration must be between 1 and 1440 minutes (24 hours).")
        return

    prefs = await get_user_preferences(redis_client, message.from_user.id)
    prefs.muted_until = time.time() + (minutes * 60)
    await save_user_preferences(redis_client, prefs)

    if minutes < 60:
        duration_str = f"{minutes} minute(s)"
    else:
        duration_str = f"{minutes // 60} hour(s)"

    await message.reply(
        f"🔇 Notifications muted for {duration_str}\n\n"
        f"Use /unmute to turn them back on.",
        parse_mode="HTML",
    )


@router.message(Command("unmute"))
async def cmd_unmute(
    message: Message,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Unmute notifications."""
    if not redis_client:
        await message.reply("Settings service unavailable.")
        return

    prefs = await get_user_preferences(redis_client, message.from_user.id)

    if prefs.muted_until is None or prefs.muted_until < time.time():
        await message.reply("Notifications are not muted.")
        return

    prefs.muted_until = None
    await save_user_preferences(redis_client, prefs)

    await message.reply("🔔 Notifications unmuted!")


# --- Callback Query Handlers ---


@router.callback_query(SettingsAction.filter(F.action == "toggle_new_whales"))
async def callback_toggle_new_whales(
    callback: CallbackQuery,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Toggle new whale alerts."""
    if not redis_client:
        await callback.answer("Settings unavailable")
        return

    prefs = await get_user_preferences(redis_client, callback.from_user.id)
    prefs.whale_config.alert_on_new_whales = not prefs.whale_config.alert_on_new_whales
    await save_user_preferences(redis_client, prefs)

    status = "enabled" if prefs.whale_config.alert_on_new_whales else "disabled"
    await callback.answer(f"New whale alerts {status}")

    # Update message with new keyboard
    keyboard = build_whale_config_keyboard(prefs.whale_config)
    await callback.message.edit_reply_markup(reply_markup=keyboard.as_markup())


@router.callback_query(SettingsAction.filter(F.action == "toggle_unusual"))
async def callback_toggle_unusual(
    callback: CallbackQuery,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Toggle unusual activity alerts."""
    if not redis_client:
        await callback.answer("Settings unavailable")
        return

    prefs = await get_user_preferences(redis_client, callback.from_user.id)
    prefs.whale_config.alert_on_unusual_activity = (
        not prefs.whale_config.alert_on_unusual_activity
    )
    await save_user_preferences(redis_client, prefs)

    status = "enabled" if prefs.whale_config.alert_on_unusual_activity else "disabled"
    await callback.answer(f"Unusual activity alerts {status}")

    keyboard = build_whale_config_keyboard(prefs.whale_config)
    await callback.message.edit_reply_markup(reply_markup=keyboard.as_markup())


@router.callback_query(SettingsAction.filter(F.action == "toggle_accum"))
async def callback_toggle_accum(
    callback: CallbackQuery,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Toggle accumulation alerts."""
    if not redis_client:
        await callback.answer("Settings unavailable")
        return

    prefs = await get_user_preferences(redis_client, callback.from_user.id)
    prefs.whale_config.alert_on_accumulation = (
        not prefs.whale_config.alert_on_accumulation
    )
    await save_user_preferences(redis_client, prefs)

    status = "enabled" if prefs.whale_config.alert_on_accumulation else "disabled"
    await callback.answer(f"Accumulation alerts {status}")

    keyboard = build_whale_config_keyboard(prefs.whale_config)
    await callback.message.edit_reply_markup(reply_markup=keyboard.as_markup())


@router.callback_query(SettingsAction.filter(F.action == "whale_reset"))
async def callback_whale_reset(
    callback: CallbackQuery,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Reset whale config to defaults."""
    if not redis_client:
        await callback.answer("Settings unavailable")
        return

    prefs = await get_user_preferences(redis_client, callback.from_user.id)
    prefs.whale_config = WhaleDetectionConfig()
    await save_user_preferences(redis_client, prefs)

    await callback.answer("Whale detection settings reset to defaults")

    keyboard = build_whale_config_keyboard(prefs.whale_config)
    await callback.message.edit_reply_markup(reply_markup=keyboard.as_markup())


@router.callback_query(SettingsAction.filter(F.action == "interval_reset"))
async def callback_interval_reset(
    callback: CallbackQuery,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Reset intervals to defaults."""
    if not redis_client:
        await callback.answer("Settings unavailable")
        return

    prefs = await get_user_preferences(redis_client, callback.from_user.id)
    prefs.monitoring_intervals = {
        exchange: limits["default"] for exchange, limits in INTERVAL_LIMITS.items()
    }
    await save_user_preferences(redis_client, prefs)

    await callback.answer("Intervals reset to defaults")

    keyboard = build_intervals_keyboard(prefs.monitoring_intervals)
    await callback.message.edit_reply_markup(reply_markup=keyboard.as_markup())


@router.callback_query(SettingsAction.filter(F.action == "notify"))
async def callback_notify(
    callback: CallbackQuery,
    callback_data: SettingsAction,
    redis_client: Optional[RedisClient] = None,
) -> None:
    """Update notification preference."""
    if not redis_client:
        await callback.answer("Settings unavailable")
        return

    try:
        new_pref = NotificationPreference(callback_data.value)
    except ValueError:
        await callback.answer("Invalid preference")
        return

    prefs = await get_user_preferences(redis_client, callback.from_user.id)
    prefs.notification_preference = new_pref
    await save_user_preferences(redis_client, prefs)

    await callback.answer(f"Notifications: {new_pref.value.replace('_', ' ').title()}")

    # Rebuild keyboard
    builder = InlineKeyboardBuilder()
    for pref in NotificationPreference:
        emoji = "✅" if prefs.notification_preference == pref else "⬜"
        builder.button(
            text=f"{emoji} {pref.value.replace('_', ' ').title()}",
            callback_data=SettingsAction(action="notify", value=pref.value).pack(),
        )
    builder.adjust(2)

    await callback.message.edit_reply_markup(reply_markup=builder.as_markup())
