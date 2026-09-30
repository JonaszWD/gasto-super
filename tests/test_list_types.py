"""Product types on the shopping list: "Pechuga de pollo, 1 kg" priced per chain."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.models import ShoppingListItem
from app.services.compare import TypeOffer, cheapest_for_amount
from tests.compare_helpers import add_listing


def offer(price: int, qty: float | None, unit: str | None = "kg", *, by_weight: bool = False, pid: int = 1) -> TypeOffer:
    up = round(price / qty) if qty else None
    return TypeOffer(pid, pid, "dia", f"p{pid}", price, up, unit, qty, datetime.now(UTC), False, False, by_weight)


def test_cheapest_for_amount_buys_whole_packs() -> None:
    pick = cheapest_for_amount([offer(530, 0.6)], 1.0, "kg")
    assert pick is not None and pick[1:] == (1060, 2)  # 1 kg needs two 600 g packs
    assert cheapest_for_amount([offer(530, 0.6)], 0.6, "kg")[1:] == (530, 1)  # type: ignore[index]


def test_cheapest_for_amount_weighs_loose_products() -> None:
    pick = cheapest_for_amount([offer(530, 0.6, by_weight=True)], 1.0, "kg")
    assert pick is not None and pick[1:] == (883, None)  # 8,83 €/kg x 1 kg


def test_cheapest_for_amount_picks_the_cheapest_way() -> None:
    offers = [offer(350, 0.5, pid=1), offer(290, 0.4, pid=2), offer(100, None, pid=3), offer(90, 1.0, "l", pid=4)]
    pick = cheapest_for_amount(offers, 1.0, "kg")
    assert pick is not None and (pick[0].product_id, pick[1], pick[2]) == (1, 700, 2)  # 2 x 500 g beats 3 x 400 g
    assert cheapest_for_amount([offer(90, 1.0, "l")], 1.0, "kg") is None  # other units can't be measured


def _chicken(engine: Engine) -> int:
    with Session(engine) as s:
        add_listing(s, "mercadona", "m1", "Pechugas enteras de pollo", 600, size="1 kg", category="Aves y pollo")
        add_listing(s, "carrefour", "c1", "Pechuga de pollo en filetes Carrefour", 350, size="500 g")
        add_listing(s, "carrefour", "c2", "Pechuga de pollo certificado entera Carrefour", 290, size="400 g")
        add_listing(s, "dia", "d1", "Pechuga de pollo corte fino Selección de Dia 600 g aprox.", 530)
        rice = add_listing(s, "mercadona", "m9", "Arroz redondo Hacendado", 120, size="1 kg", ean="8480000110101")
        add_listing(s, "dia", "d9", "Arroz redondo Dia", 99, size="1 kg", ean="8480000110101")
        assert rice.canonical_product_id is not None
        return rice.canonical_product_id


def test_type_on_the_list(client: TestClient, clean_db: Engine) -> None:
    rice = _chicken(clean_db)
    detail = client.get("/api/compare/types/pechuga-de-pollo").json()
    assert (detail["unit"], detail["default_amount"]) == ("kg", 1.0)

    body = client.post("/api/shopping-list/types", json={"product_type": "pechuga-de-pollo", "amount": 1, "unit": "kg"}).json()
    item = body["items"][0]
    assert (item["key"], item["name"], item["name_en"], item["amount"], item["unit"]) == (
        "t:pechuga-de-pollo", "Pechuga de pollo", "Chicken breast", 1.0, "kg"
    )
    chains = {c["chain_id"]: c for c in body["chains"]}
    assert chains["mercadona"]["total_cents"] == 600  # one 1 kg pack
    carrefour = chains["carrefour"]["lines"][0]
    assert (carrefour["price_cents"], carrefour["packs"], carrefour["match"]) == (700, 2, "type")  # 2 x 500 g
    assert carrefour["chosen_name"] == "Pechuga de pollo en filetes"
    dia = chains["dia"]["lines"][0]
    assert (dia["price_cents"], dia["packs"]) == (883, None)  # "aprox": priced by weight
    assert chains["lidl"]["missing"] == 1
    assert [c["chain_id"] for c in body["chains"][:3]] == ["mercadona", "carrefour", "dia"]

    # With a specific product next to it, the split works on both kinds of line.
    body = client.post("/api/shopping-list/items", json={"product_id": rice}).json()
    assert [i["key"] for i in body["items"]] == ["t:pechuga-de-pollo", f"p:{rice}"]
    assert body["split"]["assignment"] == {"t:pechuga-de-pollo": "mercadona", f"p:{rice}": "dia"}
    assert body["split"]["total_cents"] == 600 + 99

    # Adding again adds up; the amount can be set and the type removed.
    body = client.post("/api/shopping-list/types", json={"product_type": "pechuga-de-pollo", "amount": 0.5, "unit": "kg"}).json()
    assert body["items"][0]["amount"] == 1.5
    body = client.put("/api/shopping-list/types/pechuga-de-pollo", json={"amount": 0.25}).json()
    assert body["items"][0]["amount"] == 0.25
    assert {c["chain_id"]: c["total_cents"] for c in body["chains"]}["carrefour"] == 290  # one 400 g pack now wins
    body = client.delete("/api/shopping-list/types/pechuga-de-pollo").json()
    assert [i["key"] for i in body["items"]] == [f"p:{rice}"]


def test_type_list_validation(client: TestClient) -> None:
    assert client.post("/api/shopping-list/types", json={"product_type": "nope", "amount": 1, "unit": "kg"}).json()["detail"] == "type_not_found"
    assert client.post("/api/shopping-list/types", json={"product_type": "huevos", "amount": 1, "unit": "g"}).status_code == 422
    assert client.post("/api/shopping-list/types", json={"product_type": "huevos", "amount": 0, "unit": "unit"}).status_code == 422
    assert client.put("/api/shopping-list/types/huevos", json={"amount": 1}).json()["detail"] == "item_not_found"


def test_list_item_is_a_product_or_a_type(clean_db: Engine) -> None:
    with Session(clean_db) as s:
        s.add(ShoppingListItem())
        with pytest.raises(IntegrityError):
            s.commit()
