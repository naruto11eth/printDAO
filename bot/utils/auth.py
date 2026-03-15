"""Authorization utilities for PrintDAO bot."""

import functools
import logging
from typing import Callable, Any

from aiogram.types import Message

logger = logging.getLogger(__name__)

_admin_ids: set[int] = set()


def configure_admin_ids(ids: set[int]) -> None:
    """Set the global admin user ID set. Called once at startup."""
    global _admin_ids
    _admin_ids = ids
    if ids:
        logger.info(f"Admin authorization configured for {len(ids)} user(s)")
    else:
        logger.warning("No admin user IDs configured — admin commands will be disabled")


def is_admin(user_id: int) -> bool:
    """Check if a user ID is in the admin set."""
    return user_id in _admin_ids


def admin_required(handler: Callable) -> Callable:
    """Decorator that restricts a message handler to admin users only."""

    @functools.wraps(handler)
    async def wrapper(message: Message, *args: Any, **kwargs: Any) -> Any:
        if not message.from_user or not is_admin(message.from_user.id):
            await message.reply("This command requires admin access.")
            return
        return await handler(message, *args, **kwargs)

    return wrapper
