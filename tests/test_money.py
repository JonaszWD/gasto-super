from decimal import Decimal

import pytest

from app.services.money import line_total_cents, parse_decimal, to_cents


@pytest.mark.parametrize(
    ("text", "cents"),
    [
        ("1,25", 125),
        ("1.25", 125),
        ("1,5", 150),
        ("3", 300),
        ("0,99 €", 99),
        ("1.234,56", 123456),
        ("1,234.56", 123456),
        ("1.234", 123400),
        (",5", 50),
        (2.3, 230),
        (4, 400),
    ],
)
def test_to_cents(text: str | float, cents: int) -> None:
    assert to_cents(text) == cents


@pytest.mark.parametrize("text", ["", "abc", "1,2,3a", "-1", "€"])
def test_to_cents_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        to_cents(text)


def test_quantity_three_decimals() -> None:
    assert parse_decimal("0,755", max_decimals=3) == Decimal("0.755")


def test_line_total_rounds_half_up() -> None:
    assert line_total_cents(199, 3) == 597
    assert line_total_cents(349, 0.755) == 263  # 263.495 -> 263
    assert line_total_cents(250, 0.5) == 125
    assert line_total_cents(1, 0.5) == 1
