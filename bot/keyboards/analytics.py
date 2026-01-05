"""Inline keyboards for trader analytics."""

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.filters.callback_data import CallbackData


class TraderAction(CallbackData, prefix="trader"):
    """Callback data for trader action buttons."""

    action: str  # "track", "untrack", "copy", "watchlist", "refresh", "positions", "pnl"
    address: str
    exchange: str = "hyperliquid"


class WatchlistAction(CallbackData, prefix="watchlist"):
    """Callback data for watchlist actions."""

    action: str  # "add", "remove", "list"
    address: str = ""
    group_id: str = ""


class CopyTradingAction(CallbackData, prefix="copy"):
    """Callback data for copy trading actions."""

    action: str  # "enable", "disable", "config", "signals"
    address: str = ""
    mode: str = "signals"  # "signals", "auto" (future)


def build_trader_actions_keyboard(
    address: str,
    exchange: str = "hyperliquid",
    is_tracked: bool = False,
) -> InlineKeyboardMarkup:
    """Build inline keyboard for trader analytics actions.

    Args:
        address: Wallet address
        exchange: Exchange name
        is_tracked: Whether wallet is already tracked

    Returns:
        InlineKeyboardMarkup
    """
    builder = InlineKeyboardBuilder()

    # Shorten address for callback data (64 char limit)
    addr_short = address[:20]

    # Row 1: Track/Untrack and Copy
    if is_tracked:
        builder.button(
            text="🔕 Untrack",
            callback_data=TraderAction(
                action="untrack", address=addr_short, exchange=exchange
            ).pack(),
        )
    else:
        builder.button(
            text="🔔 Track Wallet",
            callback_data=TraderAction(
                action="track", address=addr_short, exchange=exchange
            ).pack(),
        )

    builder.button(
        text="📋 Copy Signals",
        callback_data=CopyTradingAction(
            action="enable", address=addr_short, mode="signals"
        ).pack(),
    )

    # Row 2: Positions and PnL
    builder.button(
        text="📊 Positions",
        callback_data=TraderAction(
            action="positions", address=addr_short, exchange=exchange
        ).pack(),
    )
    builder.button(
        text="💰 PnL History",
        callback_data=TraderAction(
            action="pnl", address=addr_short, exchange=exchange
        ).pack(),
    )

    # Row 3: Add to Watchlist and Refresh
    builder.button(
        text="⭐ Add to Watchlist",
        callback_data=WatchlistAction(action="add", address=addr_short).pack(),
    )
    builder.button(
        text="🔄 Refresh",
        callback_data=TraderAction(
            action="refresh", address=addr_short, exchange=exchange
        ).pack(),
    )

    # Arrange: 2-2-2
    builder.adjust(2, 2, 2)
    return builder.as_markup()


def build_watchlist_keyboard(
    groups: list[dict],
    address: str = "",
) -> InlineKeyboardMarkup:
    """Build keyboard for selecting watchlist group.

    Args:
        groups: List of group dicts with 'id' and 'name'
        address: Address to add (optional)

    Returns:
        InlineKeyboardMarkup
    """
    builder = InlineKeyboardBuilder()

    addr_short = address[:20] if address else ""

    for group in groups[:8]:  # Limit to 8 groups
        builder.button(
            text=f"📁 {group['name']}",
            callback_data=WatchlistAction(
                action="add", address=addr_short, group_id=group["id"]
            ).pack(),
        )

    # Add "Create New" button
    builder.button(
        text="➕ Create New Group",
        callback_data=WatchlistAction(action="create", address=addr_short).pack(),
    )

    # Cancel button
    builder.button(
        text="❌ Cancel",
        callback_data=WatchlistAction(action="cancel").pack(),
    )

    builder.adjust(2)
    return builder.as_markup()


def build_copy_trading_keyboard(
    address: str,
    is_enabled: bool = False,
) -> InlineKeyboardMarkup:
    """Build keyboard for copy trading configuration.

    Args:
        address: Trader address
        is_enabled: Whether copy trading is enabled

    Returns:
        InlineKeyboardMarkup
    """
    builder = InlineKeyboardBuilder()
    addr_short = address[:20]

    if is_enabled:
        builder.button(
            text="🔴 Disable Copy",
            callback_data=CopyTradingAction(action="disable", address=addr_short).pack(),
        )
    else:
        builder.button(
            text="🟢 Enable Signals",
            callback_data=CopyTradingAction(
                action="enable", address=addr_short, mode="signals"
            ).pack(),
        )

    builder.button(
        text="⚙️ Configure",
        callback_data=CopyTradingAction(action="config", address=addr_short).pack(),
    )

    builder.button(
        text="📜 View Signals",
        callback_data=CopyTradingAction(action="signals", address=addr_short).pack(),
    )

    builder.button(
        text="« Back",
        callback_data=TraderAction(action="refresh", address=addr_short).pack(),
    )

    builder.adjust(2, 2)
    return builder.as_markup()


def build_confirmation_keyboard(
    action: str,
    address: str,
    exchange: str = "hyperliquid",
) -> InlineKeyboardMarkup:
    """Build yes/no confirmation keyboard.

    Args:
        action: Action to confirm
        address: Wallet address
        exchange: Exchange name

    Returns:
        InlineKeyboardMarkup
    """
    builder = InlineKeyboardBuilder()
    addr_short = address[:20]

    builder.button(
        text="✅ Yes",
        callback_data=TraderAction(
            action=f"confirm_{action}", address=addr_short, exchange=exchange
        ).pack(),
    )
    builder.button(
        text="❌ No",
        callback_data=TraderAction(
            action="cancel", address=addr_short, exchange=exchange
        ).pack(),
    )

    builder.adjust(2)
    return builder.as_markup()
