import json
from pathlib import Path

import pytest

from app.services.barcode_parser import (
    BarcodeKind,
    StoreRules,
    gtin_checksum_ok,
    load_rules,
    parse_barcode,
    store_rules_key,
)

RULES = StoreRules.model_validate(
    {
        "stores": {
            "default": [
                {
                    "prefixes": ["20", "21", "22", "23", "24", "25", "26", "27", "28", "29"],
                    "kind": "price",
                    "item_start": 2,
                    "item_length": 5,
                    "value_start": 7,
                    "value_length": 5,
                    "decimals": 2,
                }
            ],
            "lidl": [
                {
                    "prefixes": ["28"],
                    "kind": "weight",
                    "item_start": 2,
                    "item_length": 5,
                    "value_start": 7,
                    "value_length": 5,
                    "decimals": 3,
                }
            ],
            "partial": [
                {
                    "prefixes": ["29"],
                    "kind": "price",
                    "item_start": 2,
                    "item_length": 4,
                    "value_start": 8,
                    "value_length": 4,
                    "decimals": 2,
                }
            ],
        }
    }
)


def with_check_digit(body12: str) -> str:
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(body12))
    return body12 + str((10 - total % 10) % 10)


@pytest.mark.parametrize(
    "code",
    ["8480000123459", "5449000000996", "4006381333931", "96385074", "036000291452"],
)
def test_valid_checksums(code: str) -> None:
    assert gtin_checksum_ok(code)


@pytest.mark.parametrize("code", ["8480000123458", "123", "", "abcdefghijklm"])
def test_invalid_checksums(code: str) -> None:
    assert not gtin_checksum_ok(code)


def test_standard_barcode() -> None:
    parsed = parse_barcode(" 5449000000996 ", RULES)
    assert parsed.kind == BarcodeKind.STANDARD
    assert parsed.product_key == "5449000000996"
    assert parsed.valid_checksum
    assert parsed.price_cents is None


def test_variable_price_default_layout() -> None:
    code = with_check_digit("211234500349")  # item 12345, price 3,49 €
    parsed = parse_barcode(code, RULES, "mercadona")
    assert parsed.kind == BarcodeKind.VARIABLE_PRICE
    assert parsed.price_cents == 349
    assert parsed.product_key == "vw:21:12345"
    assert parsed.rules_used == "default"
    assert parsed.valid_checksum


def test_same_item_different_price_shares_product_key() -> None:
    a = parse_barcode(with_check_digit("211234500349"), RULES)
    b = parse_barcode(with_check_digit("211234501299"), RULES)
    assert a.product_key == b.product_key
    assert (a.price_cents, b.price_cents) == (349, 1299)


def test_store_specific_weight_rule() -> None:
    code = with_check_digit("280042001250")  # item 00420, 1,250 kg
    parsed = parse_barcode(code, RULES, "lidl")
    assert parsed.kind == BarcodeKind.VARIABLE_WEIGHT
    assert parsed.weight_grams == 1250
    assert parsed.price_cents is None
    assert parsed.rules_used == "lidl"


def test_store_rules_fall_back_to_default() -> None:
    code = with_check_digit("230042001250")
    parsed = parse_barcode(code, RULES, "lidl")
    assert parsed.kind == BarcodeKind.VARIABLE_PRICE
    assert parsed.price_cents == 1250


def test_custom_layout_with_price_check_digit() -> None:
    code = with_check_digit("291234500199")
    parsed = parse_barcode(code, RULES, "partial")
    assert parsed.product_key == "vw:29:1234"
    assert parsed.price_cents == 199


def test_variable_without_matching_rule() -> None:
    rules = StoreRules.model_validate({"stores": {"default": []}})
    parsed = parse_barcode(with_check_digit("211234500349"), rules)
    assert parsed.kind == BarcodeKind.VARIABLE_UNKNOWN
    assert parsed.price_cents is None
    assert parsed.product_key == "vw:21:1234500349"


def test_short_codes_starting_with_2_are_not_variable() -> None:
    assert parse_barcode("20000007", RULES).kind == BarcodeKind.STANDARD


def test_shipped_rules_file_loads() -> None:
    path = Path(__file__).resolve().parent.parent / "app" / "store_rules.json"
    rules = load_rules(path)
    assert "default" in rules.stores
    json.loads(path.read_text())  # valid JSON


def test_store_rules_key() -> None:
    assert store_rules_key("El Corte Inglés") == "el-corte-ingles"
    assert store_rules_key("Mercadona") == "mercadona"
