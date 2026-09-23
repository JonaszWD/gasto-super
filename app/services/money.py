"""Parsing of user-typed decimal numbers that may use a comma or a dot as separator."""

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

_ALLOWED = re.compile(r"^[0-9.,]+$")


def parse_decimal(value: str | int | float | Decimal, max_decimals: int = 2) -> Decimal:
    """Parse "1,25", "1.25", "1.234,56", "1,234.56" or "3" into a Decimal.

    The right-most separator is the decimal separator when it is followed by at most
    ``max_decimals`` digits; otherwise every separator is treated as a thousands mark.
    """
    if isinstance(value, bool):
        raise ValueError("invalid number")
    if isinstance(value, int | Decimal):
        result = Decimal(value)
    elif isinstance(value, float):
        result = Decimal(str(value))
    else:
        text = value.strip().replace("€", "").replace(" ", "").replace(" ", "")
        if not text or not _ALLOWED.match(text):
            raise ValueError(f"invalid number: {value!r}")
        last = max(text.rfind(","), text.rfind("."))
        if last == -1:
            int_part, frac_part = text, ""
        else:
            frac = text[last + 1 :]
            if 0 < len(frac) <= max_decimals and text.count(text[last]) == 1:
                int_part, frac_part = text[:last], frac
            else:
                int_part, frac_part = text, ""
        int_part = int_part.replace(",", "").replace(".", "")
        try:
            result = Decimal(f"{int_part or '0'}.{frac_part or '0'}")
        except InvalidOperation as exc:
            raise ValueError(f"invalid number: {value!r}") from exc
    if result < 0:
        raise ValueError("number must not be negative")
    return result


def to_cents(value: str | int | float | Decimal) -> int:
    """Euros (as typed by the user) -> integer cents."""
    amount = parse_decimal(value, max_decimals=2)
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def line_total_cents(unit_price_cents: int, quantity: float | Decimal) -> int:
    total = Decimal(unit_price_cents) * Decimal(str(quantity))
    return int(total.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
