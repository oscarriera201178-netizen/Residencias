from decimal import Decimal, InvalidOperation

import pytest

from money_format import format_money, parse_money


@pytest.mark.parametrize("text,expected", [
    ("1.234,56", "1234.56"), ("35.492,00", "35492.00"),
    ("1.250.000,50", "1250000.50"), ("1.000", "1000"),
    ("150,50", "150.50"), ("150.50", "150.50"), ("0", "0"),
])
def test_money_parsing(text, expected):
    assert parse_money(text) == Decimal(expected)


@pytest.mark.parametrize("text", ["NaN", "Infinity", "1,234.56", "12.34,56", "1,234", "", "1e3"])
def test_invalid_money_is_rejected(text):
    with pytest.raises(InvalidOperation):
        parse_money(text)


def test_money_format_round_trip():
    for amount in ("0", "1234.56", "35492", "1250000.50"):
        value = Decimal(amount)
        assert parse_money(format_money(value)) == value
    assert format_money(Decimal("35492")) == "35.492,00"
