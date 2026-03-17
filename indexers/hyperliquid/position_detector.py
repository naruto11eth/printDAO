"""Position change detection from WebSocket user events.

Compares incoming position snapshots with cached state to detect:
- New positions opened
- Positions closed
- Position size increased
- Position size decreased
"""

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PositionChange:
    """A detected position change."""
    address: str
    coin: str
    change_type: str  # "opened", "closed", "increased", "decreased"
    old_size: float
    new_size: float
    direction: str  # "LONG" or "SHORT"
    entry_price: float = 0.0
    unrealized_pnl: float = 0.0


class PositionChangeDetector:
    """Detects position changes by comparing snapshots.

    Maintains an in-memory cache of last-known positions per address.
    When a new snapshot arrives (from WS or polling), compares and
    emits PositionChange events.
    """

    def __init__(self):
        # {address: {coin: {"size": float, "direction": str}}}
        self._cache: dict[str, dict[str, dict[str, Any]]] = {}

    def detect_changes(
        self, address: str, positions: list[dict],
    ) -> list[PositionChange]:
        """Compare new positions with cached state.

        Args:
            address: Wallet address
            positions: List of position dicts from API/WS with keys:
                       coin, szi (signed size), entryPx, unrealizedPnl

        Returns:
            List of detected position changes
        """
        address_lower = address.lower()
        old_positions = self._cache.get(address_lower, {})
        new_positions: dict[str, dict[str, Any]] = {}
        changes: list[PositionChange] = []

        for pos in positions:
            coin = pos.get("coin", "")
            size = float(pos.get("szi", 0))
            if size == 0:
                continue

            direction = "LONG" if size > 0 else "SHORT"
            abs_size = abs(size)
            entry_px = float(pos.get("entryPx", 0))
            upnl = float(pos.get("unrealizedPnl", 0))

            new_positions[coin] = {
                "size": abs_size,
                "direction": direction,
                "entry_px": entry_px,
                "upnl": upnl,
            }

            if coin not in old_positions:
                changes.append(PositionChange(
                    address=address_lower,
                    coin=coin,
                    change_type="opened",
                    old_size=0,
                    new_size=abs_size,
                    direction=direction,
                    entry_price=entry_px,
                    unrealized_pnl=upnl,
                ))
            else:
                old = old_positions[coin]
                old_size = old["size"]
                if abs_size > old_size * 1.05:
                    changes.append(PositionChange(
                        address=address_lower,
                        coin=coin,
                        change_type="increased",
                        old_size=old_size,
                        new_size=abs_size,
                        direction=direction,
                        entry_price=entry_px,
                        unrealized_pnl=upnl,
                    ))
                elif abs_size < old_size * 0.95:
                    changes.append(PositionChange(
                        address=address_lower,
                        coin=coin,
                        change_type="decreased",
                        old_size=old_size,
                        new_size=abs_size,
                        direction=direction,
                        entry_price=entry_px,
                        unrealized_pnl=upnl,
                    ))

        # Detect closed positions
        for coin, old in old_positions.items():
            if coin not in new_positions:
                changes.append(PositionChange(
                    address=address_lower,
                    coin=coin,
                    change_type="closed",
                    old_size=old["size"],
                    new_size=0,
                    direction=old["direction"],
                ))

        self._cache[address_lower] = new_positions
        return changes

    def clear_cache(self, address: str | None = None) -> None:
        """Clear cached positions for an address or all."""
        if address:
            self._cache.pop(address.lower(), None)
        else:
            self._cache.clear()


def format_position_change(change: PositionChange) -> str:
    """Format a position change as HTML for Telegram alert."""
    emoji = {
        "opened": "🟢",
        "closed": "🔴",
        "increased": "⬆️",
        "decreased": "⬇️",
    }.get(change.change_type, "📊")

    addr_short = f"{change.address[:6]}...{change.address[-4:]}"

    lines = [
        f"{emoji} <b>Position {change.change_type.upper()}</b>",
        f"",
        f"<code>{addr_short}</code>",
        f"<b>{change.coin}</b> {change.direction}",
    ]

    if change.change_type == "opened":
        lines.append(f"Size: {change.new_size:,.4f}")
    elif change.change_type == "closed":
        lines.append(f"Closed size: {change.old_size:,.4f}")
    else:
        lines.append(f"Size: {change.old_size:,.4f} → {change.new_size:,.4f}")

    if change.entry_price:
        lines.append(f"Entry: ${change.entry_price:,.2f}")
    if change.unrealized_pnl:
        pnl_emoji = "🟢" if change.unrealized_pnl > 0 else "🔴"
        lines.append(f"uPnL: {pnl_emoji} ${change.unrealized_pnl:,.2f}")

    return "\n".join(lines)
