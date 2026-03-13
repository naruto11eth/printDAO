"""Whale intelligence commands — flow detection, labeling, history, alert rules."""

import logging
from typing import Optional

from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command, CommandObject

from bot.services.whale_flow_service import WhaleFlowDetector, format_whale_flow
from bot.services.label_service import LabelService, VALID_LABEL_TYPES
from bot.services.alert_rules_service import AlertRulesService, ALLOWED_METRICS, ALLOWED_OPERATORS
from bot.utils.auth import admin_required
from bot.storage.redis_client import RedisClient
from indexers.hyperliquid import HyperliquidClient
from shared.utils import is_valid_ethereum_address, shorten_address, escape_html
from shared.formatting import format_usd

logger = logging.getLogger(__name__)

router = Router(name="intelligence")


# --- Whale Flow ---

@router.message(Command("flows"))
async def cmd_flows(
    message: Message,
    whale_flow_detector: Optional[WhaleFlowDetector] = None,
) -> None:
    """Show active whale flows."""
    if not whale_flow_detector:
        await message.reply("Whale flow detector not available.")
        return

    flows = await whale_flow_detector.get_active_flows()

    if not flows:
        await message.reply("No active whale flows detected.")
        return

    lines = ["<b>Active Whale Flows</b>\n"]
    for f in flows[:10]:
        side_emoji = "🟢" if f.get("side", "").upper() in ("BUY", "LONG") else "🔴"
        lines.append(
            f"{side_emoji} <b>{f.get('asset', '?')}</b> — "
            f"{f.get('whale_count', '?')} whales, "
            f"${float(f.get('total_value', 0)):,.0f} "
            f"[{f.get('conviction', '?')}]"
        )

    await message.reply("\n".join(lines), parse_mode="HTML")


# --- Wallet Labeling ---

@router.message(Command("label"))
@admin_required
async def cmd_label(
    message: Message,
    command: CommandObject,
    label_service: Optional[LabelService] = None,
) -> None:
    """Label a wallet address (admin only).

    Usage: /label <address> <label> [type]
    """
    if not label_service:
        await message.reply("Label service not available.")
        return

    if not command.args:
        types_str = ", ".join(sorted(VALID_LABEL_TYPES))
        await message.reply(
            f"<b>Usage:</b> /label &lt;address&gt; &lt;label&gt; [type]\n\n"
            f"<b>Types:</b> {types_str}",
            parse_mode="HTML",
        )
        return

    parts = command.args.strip().split()
    if len(parts) < 2:
        await message.reply("Provide address and label.")
        return

    address = parts[0]
    label = parts[1]
    label_type = parts[2] if len(parts) > 2 else "custom"

    if not is_valid_ethereum_address(address):
        await message.reply("Invalid address.")
        return

    if label_type not in VALID_LABEL_TYPES:
        await message.reply(f"Invalid type. Use: {', '.join(sorted(VALID_LABEL_TYPES))}")
        return

    await label_service.set_label(address, label, label_type)
    await message.reply(
        f"Labeled <code>{shorten_address(address)}</code> as <b>{escape_html(label)}</b> ({label_type})",
        parse_mode="HTML",
    )


@router.message(Command("whois"))
async def cmd_whois(
    message: Message,
    command: CommandObject,
    label_service: Optional[LabelService] = None,
) -> None:
    """Look up wallet label.

    Usage: /whois <address>
    """
    if not command.args or not label_service:
        await message.reply("<b>Usage:</b> /whois &lt;address&gt;", parse_mode="HTML")
        return

    address = command.args.strip()
    if not is_valid_ethereum_address(address):
        await message.reply("Invalid address.")
        return

    data = await label_service.get_label(address)
    if data:
        await message.reply(
            f"<code>{shorten_address(address)}</code>\n"
            f"Label: <b>{escape_html(data['label'])}</b>\n"
            f"Type: {data['type']}",
            parse_mode="HTML",
        )
    else:
        await message.reply(
            f"<code>{shorten_address(address)}</code> — no label assigned.",
            parse_mode="HTML",
        )


# --- History Timeline ---

@router.message(Command("history"))
async def cmd_history(
    message: Message,
    command: CommandObject,
    hyperliquid_client: Optional[HyperliquidClient] = None,
) -> None:
    """Show whale trading history as timeline.

    Usage: /history <address> [days]
    """
    if not command.args:
        await message.reply("<b>Usage:</b> /history &lt;address&gt; [days]", parse_mode="HTML")
        return

    if not hyperliquid_client:
        await message.reply("Hyperliquid client not available.")
        return

    parts = command.args.strip().split()
    address = parts[0]
    days = 7
    if len(parts) > 1:
        try:
            days = min(90, max(1, int(parts[1])))
        except ValueError:
            pass

    if not is_valid_ethereum_address(address):
        await message.reply("Invalid address.")
        return

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        fills = await hyperliquid_client.get_user_fills(address, limit=200)
        if not fills:
            await message.reply("No trading history found.")
            return

        lines = [f"<b>Trading History — {shorten_address(address)}</b>\n"]

        shown = 0
        for fill in fills[:20]:
            pnl = float(fill.closed_pnl)
            if pnl == 0:
                continue

            pnl_emoji = "🟢" if pnl > 0 else "🔴"
            time_str = fill.datetime.strftime("%m/%d %H:%M")
            side_str = "LONG" if fill.side.value == "B" else "SHORT"

            lines.append(
                f"{pnl_emoji} <b>{fill.coin}</b> {side_str} — "
                f"{format_usd(pnl, sign=True)} "
                f"<i>{time_str}</i>"
            )
            shown += 1

        if shown == 0:
            lines.append("<i>No realized PnL in recent fills.</i>")

        total_pnl = sum(float(f.closed_pnl) for f in fills)
        total_emoji = "🟢" if total_pnl >= 0 else "🔴"
        lines.append(f"\n{total_emoji} <b>Total:</b> {format_usd(total_pnl, sign=True)}")
        lines.append(f"<i>Based on last {len(fills)} fills</i>")

        await message.reply("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error fetching history for {address}: {e}")
        await message.reply(f"Error: {str(e)}")


# --- Alert Rules ---

@router.message(Command("alert_rule"))
async def cmd_alert_rule(
    message: Message,
    command: CommandObject,
    alert_rules_service: Optional[AlertRulesService] = None,
) -> None:
    """Create a structured alert rule.

    Usage: /alert_rule <metric> <operator> <threshold> [asset] [exchange]
    Example: /alert_rule trade_value gt 1000000 BTC
    """
    if not alert_rules_service:
        await message.reply("Alert rules service not available.")
        return

    if not command.args:
        metrics_str = ", ".join(sorted(ALLOWED_METRICS))
        ops_str = ", ".join(sorted(ALLOWED_OPERATORS))
        await message.reply(
            f"<b>Usage:</b> /alert_rule &lt;metric&gt; &lt;op&gt; &lt;threshold&gt; [asset] [exchange]\n\n"
            f"<b>Metrics:</b> {metrics_str}\n"
            f"<b>Operators:</b> {ops_str}\n\n"
            f"<b>Example:</b>\n"
            f"<code>/alert_rule trade_value gt 1000000 BTC</code>",
            parse_mode="HTML",
        )
        return

    parts = command.args.strip().split()
    if len(parts) < 3:
        await message.reply("Need at least: metric, operator, threshold.")
        return

    metric = parts[0]
    operator = parts[1]
    try:
        threshold = float(parts[2].replace(",", "").replace("$", ""))
    except ValueError:
        await message.reply("Invalid threshold number.")
        return

    asset_filter = parts[3] if len(parts) > 3 else ""
    exchange_filter = parts[4] if len(parts) > 4 else ""

    result = await alert_rules_service.add_rule(
        user_id=message.from_user.id,
        metric=metric,
        operator=operator,
        threshold=threshold,
        asset_filter=asset_filter,
        exchange_filter=exchange_filter,
    )

    if isinstance(result, str):
        await message.reply(f"Error: {result}")
    else:
        await message.reply(
            f"Rule created: <code>{result.description}</code>\nID: <code>{result.id}</code>",
            parse_mode="HTML",
        )


@router.message(Command("my_rules"))
async def cmd_my_rules(
    message: Message,
    alert_rules_service: Optional[AlertRulesService] = None,
) -> None:
    """List your active alert rules."""
    if not alert_rules_service:
        await message.reply("Alert rules service not available.")
        return

    rules = await alert_rules_service.get_rules(message.from_user.id)
    if not rules:
        await message.reply("No alert rules configured.\n\nUse /alert_rule to create one.")
        return

    lines = [f"<b>Your Alert Rules ({len(rules)})</b>\n"]
    for rule in rules:
        lines.append(f"<code>{rule.id}</code> — {rule.description}")

    lines.append(f"\n<i>Use /delete_rule &lt;id&gt; to remove</i>")
    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("delete_rule"))
async def cmd_delete_rule(
    message: Message,
    command: CommandObject,
    alert_rules_service: Optional[AlertRulesService] = None,
) -> None:
    """Delete an alert rule by ID.

    Usage: /delete_rule <rule_id>
    """
    if not command.args or not alert_rules_service:
        await message.reply("<b>Usage:</b> /delete_rule &lt;id&gt;", parse_mode="HTML")
        return

    rule_id = command.args.strip()
    deleted = await alert_rules_service.delete_rule(message.from_user.id, rule_id)

    if deleted:
        await message.reply(f"Deleted rule <code>{rule_id}</code>", parse_mode="HTML")
    else:
        await message.reply("Rule not found.")
