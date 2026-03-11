"""Tests for shared.formatting module."""

from decimal import Decimal
from shared.formatting import format_usd, format_percent, format_pnl


class TestFormatUsd:
    def test_basic_positive(self):
        assert format_usd(1234.56) == "$1,234.56"

    def test_basic_negative(self):
        assert format_usd(-500.0) == "-$500.00"

    def test_zero(self):
        assert format_usd(0) == "$0.00"

    def test_with_sign_positive(self):
        assert format_usd(100, sign=True) == "+$100.00"

    def test_with_sign_negative(self):
        assert format_usd(-100, sign=True) == "-$100.00"

    def test_with_sign_zero(self):
        assert format_usd(0, sign=True) == "+$0.00"

    def test_short_millions(self):
        assert format_usd(1_500_000, short=True) == "$1.50M"

    def test_short_thousands(self):
        assert format_usd(45_000, short=True) == "$45.0K"

    def test_short_small(self):
        assert format_usd(999, short=True) == "$999.00"

    def test_short_negative(self):
        assert format_usd(-2_500_000, short=True) == "-$2.50M"

    def test_decimal_input(self):
        assert format_usd(Decimal("1234.56")) == "$1,234.56"

    def test_sign_and_short(self):
        result = format_usd(1_000_000, sign=True, short=True)
        assert result == "+$1.00M"

    def test_int_input(self):
        assert format_usd(100) == "$100.00"


class TestFormatPercent:
    def test_positive_with_sign(self):
        assert format_percent(12.5) == "+12.50%"

    def test_negative_with_sign(self):
        assert format_percent(-3.2) == "-3.20%"

    def test_zero(self):
        assert format_percent(0) == "0.00%"

    def test_no_sign(self):
        assert format_percent(5.0, sign=False) == "5.00%"

    def test_custom_decimals(self):
        assert format_percent(12.345, decimals=1) == "+12.3%"

    def test_decimal_input(self):
        assert format_percent(Decimal("5.5")) == "+5.50%"


class TestFormatPnl:
    def test_positive_pnl(self):
        result = format_pnl(1500, 12.5)
        assert "+$1,500.00" in result
        assert "+12.5%" in result

    def test_negative_pnl(self):
        result = format_pnl(-800, -5.2)
        assert "-$800.00" in result
        assert "-5.2%" in result

    def test_zero_pnl(self):
        result = format_pnl(0, 0)
        assert "+$0.00" in result

    def test_decimal_inputs(self):
        result = format_pnl(Decimal("100"), Decimal("2.5"))
        assert "+$100.00" in result
