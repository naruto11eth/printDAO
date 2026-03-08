"""Consolidated formatting functions for PrintDAO.

Single source of truth for USD, percentage, and PnL formatting.
Handles both Decimal and float inputs.
"""

from decimal import Decimal


def _to_float(value: Decimal | float | int) -> float:
    return float(value) if isinstance(value, Decimal) else float(value)


def format_usd(value: Decimal | float | int, *, sign: bool = False, short: bool = False) -> str:
    """Format a value as USD.

    Args:
        value: Numeric value
        sign: If True, prepend +/- sign
        short: If True, use K/M suffixes
    """
    val = _to_float(value)

    if short:
        if abs(val) >= 1_000_000:
            formatted = f"${abs(val) / 1_000_000:,.2f}M"
        elif abs(val) >= 1_000:
            formatted = f"${abs(val) / 1_000:,.1f}K"
        else:
            formatted = f"${abs(val):,.2f}"
    else:
        formatted = f"${abs(val):,.2f}"

    if sign:
        prefix = "+" if val >= 0 else "-"
        return f"{prefix}{formatted}"
    elif val < 0:
        return f"-{formatted}"
    return formatted


def format_percent(value: Decimal | float | int, *, sign: bool = True, decimals: int = 2) -> str:
    """Format a value as percentage.

    Args:
        value: Numeric value (e.g., 12.5 for 12.5%)
        sign: If True, prepend +/- sign
        decimals: Decimal places
    """
    val = _to_float(value)
    if sign:
        prefix = "+" if val > 0 else ""
        return f"{prefix}{val:.{decimals}f}%"
    return f"{val:.{decimals}f}%"


def format_pnl(usd_value: Decimal | float | int, percent_value: Decimal | float | int) -> str:
    """Format PnL as signed USD with percentage in parentheses."""
    usd_str = format_usd(usd_value, sign=True)
    pct_str = format_percent(percent_value, sign=True, decimals=1)
    return f"{usd_str} ({pct_str})"
