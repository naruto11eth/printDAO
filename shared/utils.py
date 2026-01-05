"""Shared utility functions for PrintDAO."""

import re
from typing import Optional
from decimal import Decimal

from shared.constants import ETHEREUM_ADDRESS_PATTERN, PNL_PERIOD_ALIASES, VALID_PNL_PERIODS


def is_valid_ethereum_address(address: str) -> bool:
    """Validate Ethereum address format (0x + 40 hex chars)."""
    if not address:
        return False
    return bool(ETHEREUM_ADDRESS_PATTERN.match(address))


def normalize_address(address: str) -> str:
    """Normalize address to lowercase."""
    return address.lower() if address else ""


def shorten_address(address: str, prefix: int = 6, suffix: int = 4) -> str:
    """Shorten address for display (e.g., 0x742d...bEb)."""
    if not address or len(address) < prefix + suffix + 3:
        return address
    return f"{address[:prefix]}...{address[-suffix:]}"


def normalize_pnl_period(period: str) -> Optional[str]:
    """Normalize PnL period to canonical form (day, week, month, all)."""
    if not period:
        return None

    period_lower = period.lower().strip()

    # Check aliases first
    if period_lower in PNL_PERIOD_ALIASES:
        return PNL_PERIOD_ALIASES[period_lower]

    # Check direct match
    if period_lower in VALID_PNL_PERIODS:
        return period_lower

    return None


def format_usd(value: float, decimals: int = 2) -> str:
    """Format a number as USD with commas."""
    if value >= 1_000_000:
        return f"${value / 1_000_000:,.{decimals}f}M"
    elif value >= 1_000:
        return f"${value / 1_000:,.{decimals}f}K"
    else:
        return f"${value:,.{decimals}f}"


def format_percentage(value: float, decimals: int = 2) -> str:
    """Format a number as percentage."""
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.{decimals}f}%"


def to_decimal(value) -> Decimal:
    """Convert value to Decimal safely."""
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def parse_command_args(args: str) -> dict:
    """Parse command arguments like 'address label --exchange=polymarket'."""
    result = {
        "positional": [],
        "flags": {},
    }

    if not args:
        return result

    parts = args.split()
    for part in parts:
        if part.startswith("--"):
            # Flag argument
            if "=" in part:
                key, value = part[2:].split("=", 1)
                result["flags"][key] = value
            else:
                result["flags"][part[2:]] = True
        else:
            result["positional"].append(part)

    return result


def escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram."""
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
