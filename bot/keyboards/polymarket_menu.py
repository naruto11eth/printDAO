"""Inline keyboards for Polymarket menu navigation."""

from typing import Optional
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.filters.callback_data import CallbackData

from indexers.polymarket import Market, Tag


class PMMenuAction(CallbackData, prefix="pm_menu"):
    """Main menu navigation."""

    action: str  # "top", "search", "categories", "refresh", "back"
    page: int = 0


class PMMarketAction(CallbackData, prefix="pm_mkt"):
    """Market-specific actions."""

    action: str  # "view", "positions", "news", "back"
    market_id: str  # slug (truncated to 45 chars if needed)
    page: int = 0


class PMCategoryAction(CallbackData, prefix="pm_cat"):
    """Category browsing."""

    action: str  # "select", "back"
    tag_slug: str = ""
    page: int = 0


class PMSearchAction(CallbackData, prefix="pm_search"):
    """Search actions."""

    action: str  # "query", "results", "back"
    query: str = ""
    page: int = 0


# ============== Keyboard Builders ==============


def build_pm_main_menu() -> InlineKeyboardMarkup:
    """Build main Polymarket menu keyboard."""
    builder = InlineKeyboardBuilder()

    # Row 1: Top Markets | Search
    builder.button(
        text="🔥 Top Markets",
        callback_data=PMMenuAction(action="top", page=0).pack(),
    )
    builder.button(
        text="🔍 Search",
        callback_data=PMMenuAction(action="search").pack(),
    )

    # Row 2: Categories | Refresh
    builder.button(
        text="📁 Categories",
        callback_data=PMMenuAction(action="categories").pack(),
    )
    builder.button(
        text="🔄 Refresh",
        callback_data=PMMenuAction(action="refresh").pack(),
    )

    builder.adjust(2, 2)
    return builder.as_markup()


def build_pm_top_markets(
    markets: list[Market],
    page: int = 0,
    per_page: int = 10,
) -> InlineKeyboardMarkup:
    """Build keyboard for top markets list with pagination.

    Args:
        markets: List of markets to display
        page: Current page number
        per_page: Markets per page

    Returns:
        InlineKeyboardMarkup
    """
    builder = InlineKeyboardBuilder()

    # Market buttons (numbered 1-10)
    for i, market in enumerate(markets, 1):
        # Use slug for lookup (truncate to 45 chars to fit callback limit)
        market_slug = market.slug[:45] if market.slug else market.condition_id[:45]
        builder.button(
            text=f"{i}",
            callback_data=PMMarketAction(
                action="view", market_id=market_slug, page=page
            ).pack(),
        )

    # Pagination row
    has_prev = page > 0
    has_next = len(markets) == per_page  # Assume more if full page

    if has_prev:
        builder.button(
            text="◀ Prev",
            callback_data=PMMenuAction(action="top", page=page - 1).pack(),
        )

    if has_next:
        builder.button(
            text="Next ▶",
            callback_data=PMMenuAction(action="top", page=page + 1).pack(),
        )

    # Back button
    builder.button(
        text="« Back to Menu",
        callback_data=PMMenuAction(action="back").pack(),
    )

    # Adjust layout: market numbers (5 per row), then nav, then back
    if len(markets) <= 5:
        sizes = [len(markets)]
    else:
        sizes = [5, len(markets) - 5]

    if has_prev or has_next:
        sizes.append(2)
    sizes.append(1)  # Back button

    builder.adjust(*sizes)
    return builder.as_markup()


def build_pm_market_detail(
    market: Market,
    page: int = 0,
) -> InlineKeyboardMarkup:
    """Build keyboard for market detail view.

    Args:
        market: Market object
        page: Page to return to

    Returns:
        InlineKeyboardMarkup
    """
    builder = InlineKeyboardBuilder()
    # Use slug for lookup (truncate to 45 chars if needed)
    market_slug = market.slug[:45] if market.slug else market.condition_id[:45]

    # Row 1: Positions | News
    builder.button(
        text="👥 Positions",
        callback_data=PMMarketAction(action="positions", market_id=market_slug).pack(),
    )
    builder.button(
        text="📰 News",
        callback_data=PMMarketAction(action="news", market_id=market_slug).pack(),
    )

    # Row 2: Trade on Polymarket (URL)
    builder.button(
        text="🔗 Trade on Polymarket",
        url=market.url,
    )

    # Row 3: Back
    builder.button(
        text="« Back",
        callback_data=PMMenuAction(action="top", page=page).pack(),
    )

    builder.adjust(2, 1, 1)
    return builder.as_markup()


def build_pm_positions_keyboard(
    market_id: str,
    page: int = 0,
) -> InlineKeyboardMarkup:
    """Build keyboard for positions view.

    Args:
        market_id: Shortened market ID
        page: Page to return to

    Returns:
        InlineKeyboardMarkup
    """
    builder = InlineKeyboardBuilder()

    builder.button(
        text="« Back to Market",
        callback_data=PMMarketAction(action="view", market_id=market_id, page=page).pack(),
    )

    builder.adjust(1)
    return builder.as_markup()


def build_pm_categories(tags: list[Tag]) -> InlineKeyboardMarkup:
    """Build keyboard for category selection.

    Args:
        tags: List of available tags

    Returns:
        InlineKeyboardMarkup
    """
    builder = InlineKeyboardBuilder()

    # Popular categories with emojis
    category_emojis = {
        "politics": "🏛️",
        "crypto": "₿",
        "sports": "⚽",
        "entertainment": "🎬",
        "finance": "💰",
        "science": "🔬",
        "tech": "💻",
        "world": "🌍",
        "elections": "🗳️",
    }

    for tag in tags[:12]:  # Limit to 12 categories
        emoji = category_emojis.get(tag.slug.lower(), "📌")
        builder.button(
            text=f"{emoji} {tag.label}",
            callback_data=PMCategoryAction(action="select", tag_slug=tag.slug).pack(),
        )

    # Back button
    builder.button(
        text="« Back to Menu",
        callback_data=PMMenuAction(action="back").pack(),
    )

    builder.adjust(2, 2, 2, 2, 2, 2, 1)
    return builder.as_markup()


def build_pm_category_markets(
    markets: list[Market],
    tag_slug: str,
    page: int = 0,
    per_page: int = 10,
) -> InlineKeyboardMarkup:
    """Build keyboard for markets in a category.

    Args:
        markets: List of markets in category
        tag_slug: Category slug
        page: Current page
        per_page: Markets per page

    Returns:
        InlineKeyboardMarkup
    """
    builder = InlineKeyboardBuilder()

    # Market buttons
    for i, market in enumerate(markets, 1):
        # Use slug for lookup (truncate to 45 chars if needed)
        market_slug = market.slug[:45] if market.slug else market.condition_id[:45]
        builder.button(
            text=f"{i}",
            callback_data=PMMarketAction(
                action="view", market_id=market_slug, page=page
            ).pack(),
        )

    # Pagination
    has_prev = page > 0
    has_next = len(markets) == per_page

    if has_prev:
        builder.button(
            text="◀ Prev",
            callback_data=PMCategoryAction(action="select", tag_slug=tag_slug, page=page - 1).pack(),
        )

    if has_next:
        builder.button(
            text="Next ▶",
            callback_data=PMCategoryAction(action="select", tag_slug=tag_slug, page=page + 1).pack(),
        )

    # Back to categories
    builder.button(
        text="« Back to Categories",
        callback_data=PMMenuAction(action="categories").pack(),
    )

    # Adjust layout: market numbers (5 per row), then nav, then back
    if len(markets) <= 5:
        sizes = [len(markets)]
    else:
        sizes = [5, len(markets) - 5]

    if has_prev or has_next:
        sizes.append(2)
    sizes.append(1)  # Back button

    builder.adjust(*sizes)
    return builder.as_markup()


def build_pm_search_results(
    markets: list[Market],
    query: str,
    page: int = 0,
    per_page: int = 10,
) -> InlineKeyboardMarkup:
    """Build keyboard for search results.

    Args:
        markets: Search results
        query: Search query (shortened)
        page: Current page
        per_page: Results per page

    Returns:
        InlineKeyboardMarkup
    """
    builder = InlineKeyboardBuilder()

    # Market buttons
    for i, market in enumerate(markets, 1):
        # Use slug for lookup (truncate to 45 chars if needed)
        market_slug = market.slug[:45] if market.slug else market.condition_id[:45]
        builder.button(
            text=f"{i}",
            callback_data=PMMarketAction(
                action="view", market_id=market_slug, page=page
            ).pack(),
        )

    # Pagination
    has_prev = page > 0
    has_next = len(markets) == per_page
    query_short = query[:15]  # Shorten for callback

    if has_prev:
        builder.button(
            text="◀ Prev",
            callback_data=PMSearchAction(action="results", query=query_short, page=page - 1).pack(),
        )

    if has_next:
        builder.button(
            text="Next ▶",
            callback_data=PMSearchAction(action="results", query=query_short, page=page + 1).pack(),
        )

    # Back to menu
    builder.button(
        text="« Back to Menu",
        callback_data=PMMenuAction(action="back").pack(),
    )

    # Adjust layout: market numbers (5 per row), then nav, then back
    if len(markets) <= 5:
        sizes = [len(markets)]
    else:
        sizes = [5, len(markets) - 5]

    if has_prev or has_next:
        sizes.append(2)
    sizes.append(1)  # Back button

    builder.adjust(*sizes)
    return builder.as_markup()


def build_pm_news_keyboard(
    market_id: str,
    page: int = 0,
) -> InlineKeyboardMarkup:
    """Build keyboard for news view.

    Args:
        market_id: Shortened market ID
        page: Page to return to

    Returns:
        InlineKeyboardMarkup
    """
    builder = InlineKeyboardBuilder()

    builder.button(
        text="🔄 Refresh News",
        callback_data=PMMarketAction(action="news", market_id=market_id).pack(),
    )

    builder.button(
        text="« Back to Market",
        callback_data=PMMarketAction(action="view", market_id=market_id, page=page).pack(),
    )

    builder.adjust(1, 1)
    return builder.as_markup()
