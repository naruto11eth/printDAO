"""Wallet tracking commands router."""

import logging

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command, CommandObject

from bot.services.wallet_service import WalletService
from shared.utils import shorten_address, parse_command_args, is_valid_ethereum_address
from shared.constants import EXCHANGES

logger = logging.getLogger(__name__)

router = Router(name="tracking")


@router.message(Command("track"))
async def cmd_track(message: Message, command: CommandObject, wallet_service: WalletService) -> None:
    """Track a wallet address.

    Usage: /track <address> [label] [--exchange=all]

    Examples:
        /track 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb
        /track 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb whale_trader
        /track 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb --exchange=polymarket
    """
    if not command.args:
        await message.reply(
            "<b>Usage:</b> /track &lt;address&gt; [label] [--exchange=all]\n\n"
            "<b>Examples:</b>\n"
            "<code>/track 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb</code>\n"
            "<code>/track 0x742d... whale_trader</code>\n"
            "<code>/track 0x742d... --exchange=polymarket</code>\n\n"
            f"<b>Exchanges:</b> {', '.join(EXCHANGES)}, or 'all' (default)",
            parse_mode="HTML",
        )
        return

    # Parse arguments
    parsed = parse_command_args(command.args)

    if not parsed["positional"]:
        await message.reply("Please provide a wallet address to track.")
        return

    address = parsed["positional"][0]
    label = parsed["positional"][1] if len(parsed["positional"]) > 1 else None
    exchange = parsed["flags"].get("exchange", "all")

    # Track wallet
    result = await wallet_service.track_wallet(
        user_id=message.from_user.id,
        address=address,
        label=label,
        exchange=exchange,
    )

    if result.success:
        wallet = result.data
        short_addr = shorten_address(wallet.address)
        label_str = f" ({wallet.label})" if wallet.label else ""
        exchange_str = f" [{wallet.exchange}]" if wallet.exchange != "all" else ""

        await message.reply(
            f"✅ Now tracking: <code>{short_addr}</code>{label_str}{exchange_str}\n\n"
            f"You'll receive alerts when this wallet trades.\n"
            f"Use /list to see all tracked wallets.",
            parse_mode="HTML",
        )
    else:
        await message.reply(f"❌ {result.error}")


@router.message(Command("untrack"))
async def cmd_untrack(message: Message, command: CommandObject, wallet_service: WalletService) -> None:
    """Stop tracking a wallet.

    Usage: /untrack <address|label>

    Examples:
        /untrack 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb
        /untrack whale_trader
    """
    if not command.args:
        await message.reply(
            "<b>Usage:</b> /untrack &lt;address or label&gt;\n\n"
            "<b>Examples:</b>\n"
            "<code>/untrack 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb</code>\n"
            "<code>/untrack whale_trader</code>",
            parse_mode="HTML",
        )
        return

    identifier = command.args.strip()

    result = await wallet_service.untrack_wallet(
        user_id=message.from_user.id,
        identifier=identifier,
    )

    if result.success:
        wallet = result.data
        short_addr = shorten_address(wallet.address)
        label_str = f" ({wallet.label})" if wallet.label else ""

        await message.reply(
            f"✅ Stopped tracking: <code>{short_addr}</code>{label_str}\n\n"
            f"Use /track to start tracking again.",
            parse_mode="HTML",
        )
    else:
        await message.reply(f"❌ {result.error}")


@router.message(Command("list"))
async def cmd_list(message: Message, command: CommandObject, wallet_service: WalletService) -> None:
    """List all tracked wallets.

    Usage: /list [--exchange=all] [--group=<group_name>]

    Examples:
        /list
        /list --exchange=hyperliquid
        /list --group=sports_whales
    """
    # Parse optional filters
    exchange_filter = None
    group_filter = None

    if command.args:
        parsed = parse_command_args(command.args)
        exchange_filter = parsed["flags"].get("exchange")
        group_filter = parsed["flags"].get("group")

    # Get group ID if filtering by group name
    group_id = None
    if group_filter:
        group = await wallet_service.get_group_by_name(
            message.from_user.id, group_filter
        )
        if group:
            group_id = group.id
        else:
            await message.reply(f"❌ Group not found: {group_filter}")
            return

    wallets = await wallet_service.get_tracked_wallets(
        user_id=message.from_user.id,
        exchange=exchange_filter,
        group_id=group_id,
    )

    if not wallets:
        filter_text = ""
        if exchange_filter:
            filter_text = f" for {exchange_filter}"
        if group_filter:
            filter_text = f" in group '{group_filter}'"

        await message.reply(
            f"You're not tracking any wallets{filter_text}.\n\n"
            f"Use /track &lt;address&gt; to start tracking.",
            parse_mode="HTML",
        )
        return

    # Format wallet list
    lines = ["<b>📋 Your Tracked Wallets:</b>\n"]

    for i, wallet in enumerate(wallets, 1):
        short_addr = shorten_address(wallet.address)
        label_str = f" ({wallet.label})" if wallet.label else ""
        exchange_str = f" [{wallet.exchange}]" if wallet.exchange != "all" else ""

        lines.append(f"{i}. <code>{short_addr}</code>{label_str}{exchange_str}")

    # Add summary
    lines.append(f"\n<i>Total: {len(wallets)} wallet(s)</i>")

    # Add hint for filters
    if not exchange_filter and not group_filter:
        lines.append("\n<i>Tip: Use /list --exchange=polymarket to filter</i>")

    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("creategroup"))
async def cmd_creategroup(message: Message, command: CommandObject, wallet_service: WalletService) -> None:
    """Create a wallet group.

    Usage: /creategroup <name> [description]

    Examples:
        /creategroup sports_whales
        /creategroup top_traders Best performing wallets
    """
    if not command.args:
        await message.reply(
            "<b>Usage:</b> /creategroup &lt;name&gt; [description]\n\n"
            "<b>Examples:</b>\n"
            "<code>/creategroup sports_whales</code>\n"
            "<code>/creategroup top_traders Best performing wallets</code>",
            parse_mode="HTML",
        )
        return

    parts = command.args.split(maxsplit=1)
    name = parts[0]
    description = parts[1] if len(parts) > 1 else None

    result = await wallet_service.create_group(
        user_id=message.from_user.id,
        name=name,
        description=description,
    )

    if result.success:
        group = result.data
        await message.reply(
            f"✅ Created group: <b>{group.name}</b>\n"
            f"ID: <code>{group.id}</code>\n\n"
            f"Use /addtogroup {group.name} &lt;wallet&gt; to add wallets.",
            parse_mode="HTML",
        )
    else:
        await message.reply(f"❌ {result.error}")


@router.message(Command("groups"))
async def cmd_groups(message: Message, wallet_service: WalletService) -> None:
    """List all wallet groups."""
    groups = await wallet_service.get_groups(message.from_user.id)

    if not groups:
        await message.reply(
            "You don't have any wallet groups.\n\n"
            "Use /creategroup &lt;name&gt; to create one.",
            parse_mode="HTML",
        )
        return

    lines = ["<b>📁 Your Wallet Groups:</b>\n"]

    for group in groups:
        wallet_count = len(group.wallet_addresses)
        desc = f" - {group.description}" if group.description else ""
        lines.append(
            f"<b>{group.name}</b> (<code>{group.id}</code>)\n"
            f"  {wallet_count} wallet(s){desc}"
        )

    lines.append(f"\n<i>Use /groupinfo &lt;name&gt; to see wallets in a group</i>")

    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("groupinfo"))
async def cmd_groupinfo(message: Message, command: CommandObject, wallet_service: WalletService) -> None:
    """Show wallets in a group.

    Usage: /groupinfo <group_name>
    """
    if not command.args:
        await message.reply("Usage: /groupinfo &lt;group_name&gt;", parse_mode="HTML")
        return

    group_name = command.args.strip()

    group = await wallet_service.get_group_by_name(message.from_user.id, group_name)

    if not group:
        await message.reply(f"❌ Group not found: {group_name}")
        return

    lines = [
        f"<b>📁 Group: {group.name}</b>",
        f"ID: <code>{group.id}</code>",
    ]

    if group.description:
        lines.append(f"Description: {group.description}")

    lines.append(f"\n<b>Wallets ({len(group.wallet_addresses)}):</b>")

    if group.wallet_addresses:
        for i, address in enumerate(group.wallet_addresses, 1):
            short_addr = shorten_address(address)
            lines.append(f"{i}. <code>{short_addr}</code>")
    else:
        lines.append("<i>No wallets in this group yet.</i>")

    lines.append(f"\n<i>Use /addtogroup {group.name} &lt;wallet&gt; to add more</i>")

    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("addtogroup"))
async def cmd_addtogroup(message: Message, command: CommandObject, wallet_service: WalletService) -> None:
    """Add a wallet to a group.

    Usage: /addtogroup <group_name> <wallet_address>

    Example: /addtogroup sports_whales 0x742d...
    """
    if not command.args:
        await message.reply(
            "<b>Usage:</b> /addtogroup &lt;group_name&gt; &lt;wallet_address&gt;\n\n"
            "<b>Example:</b>\n"
            "<code>/addtogroup sports_whales 0x742d...</code>",
            parse_mode="HTML",
        )
        return

    parts = command.args.split()
    if len(parts) < 2:
        await message.reply("Please provide both group name and wallet address.")
        return

    group_name = parts[0]
    address = parts[1]

    result = await wallet_service.add_wallet_to_group(
        user_id=message.from_user.id,
        group_identifier=group_name,
        address=address,
    )

    if result.success:
        group = result.data
        short_addr = shorten_address(address)
        await message.reply(
            f"✅ Added <code>{short_addr}</code> to group <b>{group.name}</b>",
            parse_mode="HTML",
        )
    else:
        await message.reply(f"❌ {result.error}")


@router.message(Command("deletegroup"))
async def cmd_deletegroup(message: Message, command: CommandObject, wallet_service: WalletService) -> None:
    """Delete a wallet group.

    Usage: /deletegroup <group_name>
    """
    if not command.args:
        await message.reply("Usage: /deletegroup &lt;group_name&gt;", parse_mode="HTML")
        return

    group_name = command.args.strip()

    result = await wallet_service.delete_group(
        user_id=message.from_user.id,
        group_identifier=group_name,
    )

    if result.success:
        group = result.data
        await message.reply(
            f"✅ Deleted group: <b>{group.name}</b>\n\n"
            f"Wallets in this group are still being tracked.",
            parse_mode="HTML",
        )
    else:
        await message.reply(f"❌ {result.error}")


# --- Callback Query Handlers ---

@router.callback_query(F.data.startswith("track:"))
async def callback_track(callback: CallbackQuery, wallet_service: WalletService) -> None:
    """Handle track button callback from whale alerts."""
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer("Invalid callback data")
        return

    address = parts[1]
    exchange = parts[2]

    result = await wallet_service.track_wallet(
        user_id=callback.from_user.id,
        address=address,
        exchange=exchange,
    )

    if result.success:
        short_addr = shorten_address(address)
        await callback.answer(f"Now tracking {short_addr}")
        await callback.message.reply(
            f"✅ User {callback.from_user.first_name} started tracking "
            f"<code>{short_addr}</code>",
            parse_mode="HTML",
        )
    else:
        await callback.answer(result.error, show_alert=True)


@router.callback_query(F.data.startswith("copy:"))
async def callback_copy(callback: CallbackQuery) -> None:
    """Handle copy address button callback."""
    parts = callback.data.split(":")
    if len(parts) < 2:
        await callback.answer("Invalid callback data")
        return

    address = parts[1]
    # Send address in a way that's easy to copy
    await callback.answer(f"Address: {address}", show_alert=True)
