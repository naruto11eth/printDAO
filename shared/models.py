"""Shared domain models for PrintDAO."""

from dataclasses import fields
from enum import StrEnum
from typing import TypeVar, Type

T = TypeVar("T")


def safe_from_dict(cls: Type[T], data: dict) -> T:
    """Create a dataclass instance from a dict, filtering unknown fields.

    Prevents crashes when Redis data has extra fields from old schema versions.
    """
    known = {f.name for f in fields(cls)}
    filtered = {k: v for k, v in data.items() if k in known}
    return cls(**filtered)


class Exchange(StrEnum):
    """Supported exchanges."""
    HYPERLIQUID = "hyperliquid"
    POLYMARKET = "polymarket"

    @classmethod
    def from_alias(cls, value: str) -> "Exchange":
        """Parse exchange from user input, supporting aliases like 'hl' and 'pm'."""
        aliases = {
            "hl": cls.HYPERLIQUID,
            "hyper": cls.HYPERLIQUID,
            "hyperliquid": cls.HYPERLIQUID,
            "pm": cls.POLYMARKET,
            "poly": cls.POLYMARKET,
            "polymarket": cls.POLYMARKET,
        }
        normalized = value.strip().lower()
        result = aliases.get(normalized)
        if result is None:
            raise ValueError(f"Unknown exchange: {value!r}. Use: hyperliquid, polymarket")
        return result
