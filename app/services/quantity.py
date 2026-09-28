"""Parse pack sizes ("6 x 1 L", "pack 4 x 125 g", "500 g", "12 uds") into a base amount.

Base units: kilograms ("kg"), litres ("l") or units ("unit"), so any two listings in the same
base unit can be compared by unit price (€/kg, €/L, €/unidad).
"""

import re
from dataclasses import dataclass

from app.services.text import strip_accents

# unit -> (base unit, factor to base)
UNITS: dict[str, tuple[str, float]] = {
    "kg": ("kg", 1.0),
    "kgs": ("kg", 1.0),
    "kilo": ("kg", 1.0),
    "kilos": ("kg", 1.0),
    "g": ("kg", 0.001),
    "gr": ("kg", 0.001),
    "grs": ("kg", 0.001),
    "gramos": ("kg", 0.001),
    "mg": ("kg", 0.000001),
    "l": ("l", 1.0),
    "lt": ("l", 1.0),
    "lts": ("l", 1.0),
    "litro": ("l", 1.0),
    "litros": ("l", 1.0),
    "cl": ("l", 0.01),
    "ml": ("l", 0.001),
    "ud": ("unit", 1.0),
    "uds": ("unit", 1.0),
    "u": ("unit", 1.0),
    "un": ("unit", 1.0),
    "unidad": ("unit", 1.0),
    "unidades": ("unit", 1.0),
    "rollos": ("unit", 1.0),
    "lavados": ("unit", 1.0),
    "huevos": ("unit", 1.0),
    "capsulas": ("unit", 1.0),
    "bolsitas": ("unit", 1.0),
}

_NUM = r"\d+(?:[.,]\d+)?"
_UNIT = "|".join(sorted(UNITS, key=len, reverse=True))
# "6 x 1 L", "4x125g", "2 x 330 ml", optionally with words in between ("6 botellas x 1,5 L").
_MULTI = re.compile(rf"(\d+)\s*(?:[a-z]+\s*)?[x×*]\s*({_NUM})\s*({_UNIT})\b")
_SINGLE = re.compile(rf"({_NUM})\s*({_UNIT})\b")
_COUNT_WORDS = r"(?:unidades|unidad|uds|ud|u|latas|botellas|briks|bricks|tarrinas|sobres|piezas)"
# "6 unidades de 100 g", "6 uds 1 l", "4 latas de 330 ml"
_COUNT_OF = re.compile(rf"(\d+)\s*{_COUNT_WORDS}\s*(?:de\s*|x\s*)?({_NUM})\s*({_UNIT})\b")
_PACK = re.compile(r"\b(?:pack|paquete|caja|lote)\s*(?:de\s*)?-?\s*(\d+)\b")
_DOZEN = re.compile(r"\b(media\s+)?docena\b")


@dataclass(frozen=True)
class Quantity:
    value: float  # amount in the base unit
    unit: str  # kg | l | unit


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def _to_base(amount: float, unit: str) -> Quantity:
    base, factor = UNITS[unit]
    return Quantity(round(amount * factor, 6), base)


def pack_quantity(text: str | None) -> Quantity | None:
    """Total of an explicit "6 x 200 ml" style pack in `text`, or None if there isn't one."""
    if not text:
        return None
    m = _MULTI.search(strip_accents(text).lower().replace("\u00a0", " "))
    if m and int(m.group(1)) > 0:
        return _to_base(int(m.group(1)) * _num(m.group(2)), m.group(3))
    return None


def parse_quantity(text: str | None) -> Quantity | None:
    """Return the total amount described by `text`, or None if nothing recognisable."""
    if not text:
        return None
    pack = pack_quantity(text)
    if pack:
        return pack
    s = strip_accents(text).lower()
    s = s.replace("\u00a0", " ")

    m = _COUNT_OF.search(s)
    if m and UNITS[m.group(3)][0] != "unit":
        return _to_base(int(m.group(1)) * _num(m.group(2)), m.group(3))

    singles = list(_SINGLE.finditer(s))
    # When both appear, a weight or volume is more comparable than a unit count.
    m = next((x for x in singles if UNITS[x.group(2)][0] != "unit"), singles[0] if singles else None)
    if m:
        q = _to_base(_num(m.group(1)), m.group(2))
        pack = _PACK.search(s)
        # "Pack 4 yogures 125 g" style: multiply only when the pack count precedes the size.
        if pack and pack.start() < m.start() and q.unit != "unit":
            q = Quantity(round(q.value * int(pack.group(1)), 6), q.unit)
        return q

    m = _DOZEN.search(s)
    if m:
        return Quantity(6.0 if m.group(1) else 12.0, "unit")
    pack = _PACK.search(s)
    if pack:
        return Quantity(float(pack.group(1)), "unit")
    return None


def unit_price_cents(price_cents: int, quantity: Quantity | None) -> int | None:
    """Price per kg / l / unit in cents."""
    if quantity is None or quantity.value <= 0:
        return None
    return round(price_cents / quantity.value)


UNIT_LABELS = {"kg": "kg", "l": "L", "unit": "ud"}
