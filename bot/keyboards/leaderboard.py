"""Inline keyboards for leaderboard navigation."""

from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder


class LeaderboardCB(CallbackData, prefix="lb"):
    """Compact callback data for leaderboard pagination."""
    action: str  # "p"=prev, "n"=next, "s"=select trader
    page: int = 0
    idx: int = 0  # trader index on current page (for select)


class TraderDetailCB(CallbackData, prefix="td"):
    """Callback for trader detail actions from leaderboard."""
    action: str  # "a"=analytics, "t"=track, "b"=back
    addr_prefix: str = ""  # first 20 chars of address (fits 64 byte limit)
    page: int = 0


def build_leaderboard_keyboard(
    page: int, total_pages: int, entries: list[dict],
) -> InlineKeyboardBuilder:
    """Build pagination + trader select keyboard."""
    builder = InlineKeyboardBuilder()

    for i, entry in enumerate(entries):
        addr = entry.get("address", "")
        addr_short = f"{addr[:6]}...{addr[-4:]}" if len(addr) > 10 else addr
        builder.button(
            text=f"#{entry.get('rank', '?')} {addr_short}",
            callback_data=TraderDetailCB(action="a", addr_prefix=addr[:20], page=page),
        )

    builder.adjust(1)

    nav_row = []
    if page > 0:
        nav_row.append(
            LeaderboardCB(action="p", page=page - 1)
        )
    if page < total_pages - 1:
        nav_row.append(
            LeaderboardCB(action="n", page=page + 1)
        )

    if nav_row:
        for cb in nav_row:
            text = "<< Prev" if cb.action == "p" else "Next >>"
            builder.button(text=text, callback_data=cb)

    return builder
