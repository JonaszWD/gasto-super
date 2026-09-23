"""Barcode normalisation, checksum validation and variable-weight (in-store) label parsing.

EAN-13 codes with prefixes 20-29 are reserved for in-store use. Supermarkets print them
on weighed products (fruit, meat, deli...) and embed either the price or the weight.
The layout differs per chain, so it is described by rules in ``store_rules.json``::

    {
      "stores": {
        "default": [
          {"prefixes": ["20", "21"], "kind": "price",
           "item_start": 2, "item_length": 5,
           "value_start": 7, "value_length": 5, "decimals": 2}
        ]
      }
    }

``value_*`` positions are 0-based offsets into the 13 digit code. ``kind`` is ``price``
(value in euros with ``decimals`` decimals) or ``weight`` (value in kg with ``decimals``
decimals, i.e. 3 for grams).
"""

import json
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class BarcodeKind(StrEnum):
    STANDARD = "standard"
    VARIABLE_PRICE = "variable_price"
    VARIABLE_WEIGHT = "variable_weight"
    VARIABLE_UNKNOWN = "variable_unknown"


class VariableWeightRule(BaseModel):
    prefixes: list[str]
    kind: Literal["price", "weight"]
    item_start: int = Field(ge=0, le=12)
    item_length: int = Field(ge=1, le=12)
    value_start: int = Field(ge=0, le=12)
    value_length: int = Field(ge=1, le=12)
    decimals: int = Field(ge=0, le=4)


class StoreRules(BaseModel):
    stores: dict[str, list[VariableWeightRule]]


@dataclass(frozen=True)
class ParsedBarcode:
    raw: str
    code: str
    kind: BarcodeKind
    valid_checksum: bool
    product_key: str
    price_cents: int | None = None
    weight_grams: int | None = None
    rules_used: str | None = None

    @property
    def is_variable(self) -> bool:
        return self.kind != BarcodeKind.STANDARD


def normalize(raw: str) -> str:
    return re.sub(r"\D", "", raw or "")


def gtin_checksum_ok(code: str) -> bool:
    """Validate the GS1 mod-10 check digit (EAN-8, UPC-A, EAN-13, GTIN-14)."""
    if len(code) not in (8, 12, 13, 14) or not code.isdigit():
        return False
    digits = [int(d) for d in code]
    body, check = digits[:-1], digits[-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def is_variable_weight(code: str) -> bool:
    return len(code) == 13 and code[0] == "2"


def load_rules(path: Path) -> StoreRules:
    return StoreRules.model_validate(json.loads(path.read_text(encoding="utf-8")))


@lru_cache
def load_rules_cached(path: Path) -> StoreRules:
    return load_rules(path)


def _find_rule(code: str, rules: StoreRules, store_key: str | None) -> tuple[VariableWeightRule, str] | None:
    candidates = [k for k in (store_key, "default") if k and k in rules.stores]
    for key in candidates:
        for rule in rules.stores[key]:
            if any(code.startswith(p) for p in rule.prefixes):
                return rule, key
    return None


def parse_barcode(raw: str, rules: StoreRules, store_key: str | None = None) -> ParsedBarcode:
    code = normalize(raw)
    valid = gtin_checksum_ok(code)
    if not is_variable_weight(code):
        return ParsedBarcode(raw=raw, code=code, kind=BarcodeKind.STANDARD, valid_checksum=valid, product_key=code)

    found = _find_rule(code, rules, store_key)
    if found is None:
        # In-store code with no matching layout: key on everything but the check digit.
        return ParsedBarcode(
            raw=raw,
            code=code,
            kind=BarcodeKind.VARIABLE_UNKNOWN,
            valid_checksum=valid,
            product_key=f"vw:{code[:2]}:{code[2:12]}",
        )

    rule, rules_used = found
    item = code[rule.item_start : rule.item_start + rule.item_length]
    value_digits = code[rule.value_start : rule.value_start + rule.value_length]
    value = Decimal(int(value_digits)).scaleb(-rule.decimals)
    key = f"vw:{code[:2]}:{item}"
    if rule.kind == "price":
        return ParsedBarcode(
            raw=raw,
            code=code,
            kind=BarcodeKind.VARIABLE_PRICE,
            valid_checksum=valid,
            product_key=key,
            price_cents=int(value * 100),
            rules_used=rules_used,
        )
    return ParsedBarcode(
        raw=raw,
        code=code,
        kind=BarcodeKind.VARIABLE_WEIGHT,
        valid_checksum=valid,
        product_key=key,
        weight_grams=int(value * 1000),
        rules_used=rules_used,
    )


def store_rules_key(store_name: str) -> str:
    """Default rules key for a store name: 'El Corte Inglés' -> 'el-corte-ingles'."""
    ascii_name = unicodedata.normalize("NFKD", store_name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
