"""Tests for shared utilities, models, and resilience."""

import asyncio
import time

import pytest

from shared.utils import (
    is_valid_ethereum_address,
    normalize_address,
    shorten_address,
    escape_html,
    normalize_pnl_period,
)
from shared.models import Exchange, safe_from_dict
from indexers.base import BoundedDedup
from shared.resilience import CircuitBreaker, CircuitOpenError, CircuitState


class TestAddressValidation:
    def test_valid_address(self):
        assert is_valid_ethereum_address("0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb0")

    def test_invalid_no_prefix(self):
        assert not is_valid_ethereum_address("742d35Cc6634C0532925a3b844Bc9e7595f0bEb0")

    def test_invalid_too_short(self):
        assert not is_valid_ethereum_address("0x742d35Cc")

    def test_invalid_too_long(self):
        assert not is_valid_ethereum_address("0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb0aa")

    def test_invalid_non_hex(self):
        assert not is_valid_ethereum_address("0xZZZd35Cc6634C0532925a3b844Bc9e7595f0bEb0")

    def test_empty(self):
        assert not is_valid_ethereum_address("")

    def test_none_like(self):
        assert not is_valid_ethereum_address("null")


class TestNormalizeAddress:
    def test_lowercase(self):
        assert normalize_address("0xABCDEF") == "0xabcdef"


class TestShortenAddress:
    def test_shortens(self):
        result = shorten_address("0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb0")
        assert result.startswith("0x742d")
        assert result.endswith("bEb0")
        assert "..." in result


class TestEscapeHtml:
    def test_ampersand(self):
        assert escape_html("a & b") == "a &amp; b"

    def test_angle_brackets(self):
        assert escape_html("<script>") == "&lt;script&gt;"

    def test_quotes_not_escaped(self):
        assert escape_html('"hello"') == '"hello"'

    def test_no_escape_needed(self):
        assert escape_html("hello world") == "hello world"

    def test_mixed(self):
        result = escape_html("<b>test & value</b>")
        assert "&lt;" in result
        assert "&amp;" in result
        assert "&gt;" in result


class TestNormalizePnlPeriod:
    def test_aliases(self):
        assert normalize_pnl_period("1d") == "day"
        assert normalize_pnl_period("7d") == "week"
        assert normalize_pnl_period("30d") == "month"
        assert normalize_pnl_period("all") == "all"

    def test_direct(self):
        assert normalize_pnl_period("day") == "day"
        assert normalize_pnl_period("week") == "week"

    def test_unknown_returns_none(self):
        assert normalize_pnl_period("invalid") is None


class TestExchangeEnum:
    def test_values(self):
        assert Exchange.HYPERLIQUID == "hyperliquid"
        assert Exchange.POLYMARKET == "polymarket"

    def test_from_alias(self):
        assert Exchange.from_alias("hl") == Exchange.HYPERLIQUID
        assert Exchange.from_alias("pm") == Exchange.POLYMARKET
        assert Exchange.from_alias("hyperliquid") == Exchange.HYPERLIQUID
        assert Exchange.from_alias("POLYMARKET") == Exchange.POLYMARKET

    def test_from_alias_invalid(self):
        with pytest.raises(ValueError, match="Unknown exchange"):
            Exchange.from_alias("binance")


class TestSafeFromDict:
    def test_filters_unknown_fields(self):
        from dataclasses import dataclass

        @dataclass
        class Simple:
            name: str = ""
            value: int = 0

        result = safe_from_dict(Simple, {"name": "test", "value": 42, "extra": "ignored"})
        assert result.name == "test"
        assert result.value == 42
        assert not hasattr(result, "extra")

    def test_missing_fields_use_defaults(self):
        from dataclasses import dataclass

        @dataclass
        class WithDefaults:
            name: str = "default"
            count: int = 0

        result = safe_from_dict(WithDefaults, {"name": "custom"})
        assert result.name == "custom"
        assert result.count == 0

    def test_empty_dict(self):
        from dataclasses import dataclass

        @dataclass
        class AllDefaults:
            x: int = 0
            y: str = ""

        result = safe_from_dict(AllDefaults, {})
        assert result.x == 0
        assert result.y == ""


class TestBoundedDedup:
    def test_add_new_item(self):
        d = BoundedDedup(capacity=10)
        assert d.add("a") is True
        assert len(d) == 1

    def test_add_duplicate(self):
        d = BoundedDedup(capacity=10)
        d.add("a")
        assert d.add("a") is False
        assert len(d) == 1

    def test_contains(self):
        d = BoundedDedup(capacity=10)
        d.add("a")
        assert "a" in d
        assert "b" not in d

    def test_eviction_fifo(self):
        d = BoundedDedup(capacity=3)
        d.add("a")
        d.add("b")
        d.add("c")
        d.add("d")
        assert "a" not in d
        assert "d" in d
        assert len(d) == 3

    def test_eviction_order(self):
        d = BoundedDedup(capacity=2)
        d.add("first")
        d.add("second")
        d.add("third")
        assert "first" not in d
        assert "second" in d
        assert "third" in d

    def test_load_existing(self):
        d = BoundedDedup(capacity=100)
        d.load_existing(["a", "b", "c"])
        assert len(d) == 3
        assert "a" in d
        assert "c" in d

    def test_large_capacity(self):
        d = BoundedDedup(capacity=50_000)
        for i in range(100):
            d.add(str(i))
        assert len(d) == 100


class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = CircuitBreaker(name="test")
        assert cb.state == CircuitState.CLOSED

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(name="test", failure_threshold=3, recovery_timeout=60)
        cb._record_failure()
        cb._record_failure()
        assert cb.state == CircuitState.CLOSED
        cb._record_failure()
        assert cb.state == CircuitState.OPEN

    def test_resets_on_success(self):
        cb = CircuitBreaker(name="test", failure_threshold=3)
        cb._record_failure()
        cb._record_failure()
        cb._record_success()
        assert cb._failure_count == 0
        assert cb.state == CircuitState.CLOSED

    def test_half_open_after_timeout(self):
        cb = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=0.1)
        cb._record_failure()
        assert cb.state == CircuitState.OPEN
        time.sleep(0.15)
        assert cb.state == CircuitState.HALF_OPEN

    def test_open_rejects_calls(self):
        cb = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=60)
        cb._record_failure()
        with pytest.raises(CircuitOpenError):
            cb._check_can_execute()

    async def test_decorator_success(self):
        cb = CircuitBreaker(name="test")

        @cb
        async def good_func():
            return 42

        result = await good_func()
        assert result == 42
        assert cb.state == CircuitState.CLOSED

    async def test_decorator_failure_trips(self):
        cb = CircuitBreaker(name="test", failure_threshold=2)

        @cb
        async def bad_func():
            raise ConnectionError("down")

        with pytest.raises(ConnectionError):
            await bad_func()
        with pytest.raises(ConnectionError):
            await bad_func()
        assert cb.state == CircuitState.OPEN

    async def test_excluded_errors_dont_trip(self):
        cb = CircuitBreaker(name="test", failure_threshold=2, exclude=(ValueError,))

        @cb
        async def bad_input():
            raise ValueError("invalid")

        with pytest.raises(ValueError):
            await bad_input()
        with pytest.raises(ValueError):
            await bad_input()
        assert cb.state == CircuitState.CLOSED

    async def test_call_method(self):
        cb = CircuitBreaker(name="test")

        async def fn(x):
            return x * 2

        result = await cb.call(fn, 5)
        assert result == 10
