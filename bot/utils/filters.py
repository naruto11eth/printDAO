"""Filter parsing utilities for Telegram bot commands."""

import re
from typing import Optional, Tuple


def parse_num(s: str) -> float:
    """Parse number with K/M suffix."""
    s = s.strip().lower()
    if s.endswith('k'):
        return float(s[:-1]) * 1000
    elif s.endswith('m'):
        return float(s[:-1]) * 1000000
    return float(s)


def parse_size_filter(query: str) -> Tuple[Optional[float], Optional[float]]:
    """Parse size filter from query. Returns (min_size, max_size).

    Supports:
    - ">40k" or "40k+" - minimum size
    - "<100k" - maximum size
    - "10k-50k" - range
    - "40000" or "40k" - exact minimum
    """
    if not query:
        return (None, None)

    query = query.lower().replace(",", "").replace("$", "").strip()

    # >40k or 40k+
    match = re.match(r'^[>]?(\d+\.?\d*[km]?)\+?$', query)
    if match and (query.startswith('>') or query.endswith('+')):
        return (parse_num(match.group(1)), None)

    # <40k
    match = re.match(r'^<(\d+\.?\d*[km]?)$', query)
    if match:
        return (None, parse_num(match.group(1)))

    # 10k-50k
    match = re.match(r'^(\d+\.?\d*[km]?)-(\d+\.?\d*[km]?)$', query)
    if match:
        return (parse_num(match.group(1)), parse_num(match.group(2)))

    # Plain number (treat as minimum)
    match = re.match(r'^(\d+\.?\d*[km]?)$', query)
    if match:
        return (parse_num(match.group(1)), None)

    return (None, None)


def is_size_filter(query: str) -> bool:
    """Check if query is a size filter pattern."""
    if not query:
        return False
    query = query.lower().replace(",", "").replace("$", "").strip()
    # Check for size filter patterns
    return bool(re.match(r'^[><]?\d+\.?\d*[km]?[-+]?(\d+\.?\d*[km]?)?$', query))


def parse_filter_args(args: str) -> Tuple[Optional[str], Optional[float], Optional[float]]:
    """Parse filter arguments from command.

    Returns: (text_filter, min_size, max_size)

    Examples:
        "btc >40k" -> ("btc", 40000, None)
        ">40k" -> (None, 40000, None)
        "bitcoin" -> ("bitcoin", None, None)
        "10k-50k" -> (None, 10000, 50000)
    """
    if not args:
        return (None, None, None)

    parts = args.strip().split()
    text_filter = None
    min_size = None
    max_size = None

    for part in parts:
        if is_size_filter(part):
            min_s, max_s = parse_size_filter(part)
            if min_s is not None:
                min_size = min_s
            if max_s is not None:
                max_size = max_s
        else:
            # Text filter (coin name, market keyword)
            text_filter = part

    return (text_filter, min_size, max_size)


def format_filter_summary(text_filter: Optional[str], min_size: Optional[float], max_size: Optional[float]) -> str:
    """Format filter parameters as human-readable string."""
    parts = []

    if text_filter:
        parts.append(f'"{text_filter}"')

    if min_size is not None and max_size is not None:
        parts.append(f"${min_size:,.0f}-${max_size:,.0f}")
    elif min_size is not None:
        parts.append(f">${min_size:,.0f}")
    elif max_size is not None:
        parts.append(f"<${max_size:,.0f}")

    return " + ".join(parts) if parts else "all"
