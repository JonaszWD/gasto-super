"""English search terms find Spanish product names."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlmodel import Session

from app.services.synonyms import query_groups, relevance, text_matches
from tests.compare_helpers import add_listing


@pytest.mark.parametrize(
    ("query", "name", "found"),
    [
        ("milk", "Leche entera Hacendado 1 L", True),
        ("whole milk", "Leche entera Hacendado 1 L", True),
        ("skimmed milk", "Leche entera Hacendado 1 L", False),
        ("semi-skimmed milk", "Leche semidesnatada Dia 1 L", True),
        ("olive oil", "Aceite de oliva virgen extra 1 L", True),
        ("eggs", "Huevos camperos L docena", True),
        ("tomatoes", "Tomate triturado 400 g", True),
        ("chicken breast", "Pechuga de pollo fileteada", True),
        ("tea", "Aceite de girasol", False),  # "te" is matched as a whole word only
        ("tea", "Té verde 20 bolsitas", True),
        ("ham", "Hamburguesa de ternera", False),
        ("leche", "Leche entera", True),  # Spanish keeps working
        ("cafe", "Café molido natural", True),  # accents ignored
        ("lech", "Leche entera", True),  # word-start prefix
        ("eche", "Leche entera", False),  # not mid-word
    ],
)
def test_text_matches(query: str, name: str, found: bool) -> None:
    assert text_matches(name, query_groups(query)) is found


def test_query_groups_phrases() -> None:
    assert query_groups("semi skimmed milk") == [["semi skimmed", "semidesnatada", "semidesnatado"], ["milk", "leche"]]
    assert query_groups("peppers") == [["peppers", "pimienta", "pimiento"]]  # primary meaning first
    assert query_groups("  ") == []


def test_compare_search_in_english(client: TestClient, clean_db: Engine) -> None:
    with Session(clean_db) as s:
        add_listing(s, "mercadona", "m1", "Leche entera Hacendado", 95, ean="8480000123459", size="Brick 1 l")
        add_listing(s, "mercadona", "m2", "Aceite de oliva 0,4º Hacendado", 1725, size="Garrafa 5 l", ean="8402001027482")
        add_listing(s, "mercadona", "m3", "Tomate frito Hacendado", 69, ean="8480000160164")
    names = [c["product"]["name"] for c in client.get("/api/compare/search", params={"q": "milk"}).json()["results"]]
    assert names == ["Leche entera Hacendado"]
    names = [c["product"]["name"] for c in client.get("/api/compare/search", params={"q": "olive oil"}).json()["results"]]
    assert names == ["Aceite de oliva 0,4º Hacendado"]
    assert client.get("/api/compare/search", params={"q": "tea"}).json()["results"] == []


def test_tracker_product_search_in_english(client: TestClient) -> None:
    client.put("/api/products/8480000123459", json={"name": "Leche entera", "category": "Lácteos y huevos"})
    client.put("/api/products/8480000000011", json={"name": "Plátano de Canarias"})
    assert [p["name"] for p in client.get("/api/products", params={"q": "milk"}).json()] == ["Leche entera"]
    assert [p["name"] for p in client.get("/api/products", params={"q": "banana"}).json()] == ["Plátano de Canarias"]
    assert [p["name"] for p in client.get("/api/products", params={"q": "platano"}).json()] == ["Plátano de Canarias"]


def test_language_setting_and_english_export(client: TestClient) -> None:
    assert client.get("/api/settings").json() == {"postal_code": "28020", "language": "es"}
    assert client.put("/api/settings", json={"language": "en"}).json() == {"postal_code": "28020", "language": "en"}
    assert client.put("/api/settings", json={"language": "fr"}).status_code == 422
    assert client.put("/api/settings", json={"postal_code": "46001"}).json()["language"] == "en"  # partial update

    store_id = client.get("/api/stores").json()[0]["id"]
    trip = client.post("/api/trips", json={"store_id": store_id}).json()
    client.post(f"/api/trips/{trip['id']}/purchases",
                json={"barcode": "8480000123459", "name": "Leche", "category": "Lácteos y huevos", "unit_price": "0,95"})
    lines = client.get("/api/export.csv", params={"lang": "en"}).content.decode("utf-8-sig").splitlines()
    assert lines[0].startswith("date;time;store")
    assert lines[1].split(";")[6] == "Dairy & eggs"
    assert client.get("/api/export.csv").content.decode("utf-8-sig").startswith("fecha;")


def test_relevance_prefers_head_noun_and_primary_meaning() -> None:
    groups = query_groups("peppers")
    names = [
        "Pimientos del piquillo Hacendado",
        "Salmón ahumado a la pimienta",
        "Salsa pimienta verde Hacendado",
        "Pimienta negra molida Hacendado",
    ]
    ranked = sorted(names, key=lambda n: -relevance(n, "Hacendado", groups))
    assert ranked == [
        "Pimienta negra molida Hacendado",
        "Pimientos del piquillo Hacendado",
        "Salsa pimienta verde Hacendado",
        "Salmón ahumado a la pimienta",
    ]


def test_relevance_rewards_order_and_coverage() -> None:
    groups = query_groups("leche entera")
    assert relevance("Leche entera Hacendado", None, groups) > relevance("Batido de cacao con leche entera", None, groups)
    assert relevance("Leche semidesnatada", None, groups) == 0  # doesn't match every word
    # Short words only match whole words: "pan" is bread, not "panecillos"/"pantalla".
    assert relevance("Pan de molde", None, query_groups("pan")) > 0
    assert relevance("Panecillos", None, query_groups("pan")) == 0


def test_compare_search_ranks_most_relevant_first(client: TestClient, clean_db: Engine) -> None:
    with Session(clean_db) as s:
        for i, name in enumerate([
            "Pimientos del piquillo Hacendado",
            "Salmón ahumado a la pimienta",
            "Salsa pimienta verde Hacendado",
            "Pimienta negra molida Hacendado",
        ]):
            add_listing(s, "mercadona", f"p{i}", name, 100 + i, ean=None)
    names = [c["product"]["name"] for c in client.get("/api/compare/search", params={"q": "peppers"}).json()["results"]]
    assert names[0] == "Pimienta negra molida Hacendado"
    assert names[-1] == "Salmón ahumado a la pimienta"


def test_tracker_search_ranks_most_relevant_first(client: TestClient) -> None:
    client.put("/api/products/8480000000001", json={"name": "Salsa pimienta verde"})
    client.put("/api/products/8480000000002", json={"name": "Pimienta negra"})
    assert [p["name"] for p in client.get("/api/products", params={"q": "pepper"}).json()] == ["Pimienta negra", "Salsa pimienta verde"]
