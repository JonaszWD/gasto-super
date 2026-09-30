"""Product types: generic names that group products across chains ("chicken" -> Pechuga de pollo...)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlmodel import Session, select

from app.models import CanonicalProduct
from app.services.catalog import retype
from app.services.product_types import classify, load_types, types_for_query
from app.services.synonyms import query_groups
from tests.compare_helpers import add_listing


@pytest.mark.parametrize(
    ("name", "category", "slug"),
    [
        # Real names from the chains, with their categories.
        ("Pechuga de pollo corte fino Selección de Dia 600 g aprox.", "carnes/pollo/c/L2202", "pechuga-de-pollo"),
        ("Filetes de pechuga de pollo Carrefour El Mercado 600 g aprox", None, "pechuga-de-pollo"),
        ("Pechugas enteras familiar de pollo", "Aves y pollo", "pechuga-de-pollo"),
        ("Pechuga de pollo braseada Hacendado lonchas", "Aves y jamón cocido", None),  # cold cut
        ("Pechuguitas de pollo 99% Campofrío 90 g", None, None),
        ("Pollo entero Selección de Dia 2.1 Kg aprox.", "carnes/pollo/c/L2202", "pollo-entero"),
        ("Pollo asado troceado", "Platos preparados calientes", None),
        ("Contramuslos de pollo con piel y hueso Carrefour El Mercado 1 kg aprox", None, "contramuslos-de-pollo"),
        ("Muslos de pollo deshuesados con piel", "Aves y pollo", "muslos-de-pollo"),
        ("Alas de pollo barbacoa congeladas", None, None),
        ("Catit Divine Shreds - Sopas De Pollo Para Gatos 18 Sobres X 75 Gr", "Gatos", None),
        ("Leche semidesnatada Hacendado", "Leche y bebidas vegetales", "leche-semidesnatada"),
        ("Leche desnatada Central Lechera Asturiana sin lactosa brik 1 l.", None, None),  # not plain skimmed
        ("Leche entera sin lactosa Hacendado", "Leche y bebidas vegetales", "leche-sin-lactosa"),
        ("Leche condensada Nestlé 370 g", None, None),
        ("Huevos grandes L de gallinas criadas en suelo Dia 12 unidades", None, "huevos"),
        ("Huevo sorpresa de chocolate Kinder", None, None),
        ("Manzana Golden 1 Kg", "frutas/manzanas-y-peras/c/L2032", "manzanas"),
        ("Manzanilla 20 bolsitas", None, None),  # "manzana" is matched as a word, not a prefix
        ("Tomate frito Orlando 400 g", None, None),
        ("Patatas con allioli Hacendado", "Platos preparados fríos", None),
        ("Limon&Nada sin gas Minute Maid botella 1 l.", "supermercado/bebidas/refrescos/cat650001/c", None),
        ("Aceite de oliva virgen extra Hacendado", None, "aceite-de-oliva-virgen-extra"),
        ("Aceite de oliva 0,4º Hacendado", None, "aceite-de-oliva"),
    ],
)
def test_classify(name: str, category: str | None, slug: str | None) -> None:
    assert classify(name, None, category, "food") == slug


def test_classify_skips_household() -> None:
    assert classify("Huevos", None, None, "household") is None


def test_types_file_is_complete() -> None:
    types = load_types()
    assert len(types) >= 40
    for pt in types.values():
        assert pt.es and pt.en and pt.match, pt.slug
        assert all(tok for m in pt.match for tok in m), pt.slug
        assert not any(tok.startswith("@") for x in pt.exclude for tok in x), pt.slug  # shared sets expanded


def test_types_for_query() -> None:
    chicken = [pt.slug for pt in types_for_query(query_groups("chicken"))]
    assert {"pollo-entero", "pechuga-de-pollo", "alas-de-pollo"} <= set(chicken)
    assert all("pollo" in load_types()[s].es.lower() for s in chicken)
    assert [pt.slug for pt in types_for_query(query_groups("chiken brest"))] == ["pechuga-de-pollo"]  # typos too
    assert {pt.slug for pt in types_for_query(query_groups("milk"))} >= {"leche-entera", "leche-semidesnatada"}
    assert types_for_query(query_groups("detergente")) == []


def _chicken(engine: Engine) -> None:
    with Session(engine) as s:
        add_listing(s, "mercadona", "m1", "Pechugas enteras de pollo", 600, size="1 kg", category="Aves y pollo")
        add_listing(s, "carrefour", "c1", "Pechuga de pollo en filetes Carrefour", 350, size="500 g")
        add_listing(s, "carrefour", "c2", "Pechuga de pollo certificado entera Carrefour", 290, size="400 g")
        add_listing(s, "dia", "d1", "Pechuga de pollo corte fino Selección de Dia", 530, size="600 g")
        add_listing(s, "dia", "d2", "Pollo entero Selección de Dia", 700, size="2 kg")
        add_listing(s, "mercadona", "m2", "Pechuga de pollo braseada Hacendado lonchas", 250, size="200 g",
                    category="Aves y jamón cocido")


def test_products_are_typed_when_collected(clean_db: Engine) -> None:
    _chicken(clean_db)
    with Session(clean_db) as s:
        types = {p.name: p.product_type for p in s.exec(select(CanonicalProduct)).all()}
    assert types["Pechugas enteras de pollo"] == "pechuga-de-pollo"
    assert types["Pollo entero Selección de Dia"] == "pollo-entero"
    assert types["Pechuga de pollo braseada Hacendado lonchas"] is None


def test_retype_reapplies_rules(clean_db: Engine) -> None:
    _chicken(clean_db)
    with Session(clean_db) as s:
        for p in s.exec(select(CanonicalProduct)).all():
            p.product_type = "huevos" if p.product_type else None
            s.add(p)
        s.commit()
        assert retype(s) == 5
        s.commit()
        assert retype(s) == 0
        assert {p.product_type for p in s.exec(select(CanonicalProduct)).all()} == {"pechuga-de-pollo", "pollo-entero", None}


def test_types_api(client: TestClient, clean_db: Engine) -> None:
    _chicken(clean_db)
    r = client.get("/api/compare/types", params={"q": "chicken"})
    assert r.status_code == 200
    types = {t["slug"]: t for t in r.json()["types"]}
    assert set(types) == {"pechuga-de-pollo", "pollo-entero"}  # types without products are left out
    breast = types["pechuga-de-pollo"]
    assert breast["name_en"] == "Chicken breast"
    assert (breast["products"], breast["chains"], breast["unit"]) == (4, 3, "kg")
    assert (breast["min_unit_price_cents"], breast["min_chain_name"]) == (600, "Mercadona")

    r = client.get("/api/compare/types/pechuga-de-pollo")
    assert r.status_code == 200
    chains = r.json()["chains"]
    assert [c["chain_id"] for c in chains[:3]] == ["mercadona", "carrefour", "dia"]  # cheapest per kg first
    assert chains[0]["cheapest"] and not chains[1]["cheapest"]
    assert [o["unit_price_cents"] for o in chains[1]["offers"]] == [700, 725]  # cheapest product first
    assert all(not c["offers"] for c in chains[3:])  # Lidl, Alcampo: nothing of this type

    assert client.get("/api/compare/types/nope").json()["detail"] == "type_not_found"


def test_types_need_login(anon_client: TestClient) -> None:
    assert anon_client.get("/api/compare/types", params={"q": "pollo"}).status_code == 401
