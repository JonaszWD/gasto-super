"""Price comparison API: search cards, detail/history, matching actions, refresh, alternative, list."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlmodel import Session

from app.services.catalog import RawListing
from app.sources.base import SourceError
from tests.compare_helpers import add_listing

MILK_EAN = "8480000123459"


@pytest.fixture
def catalog(clean_db: Engine) -> dict[str, int]:
    now = datetime.now(UTC)
    with Session(clean_db) as s:
        merc = add_listing(s, "mercadona", "m1", "Leche entera Hacendado", 95, ean=MILK_EAN, size="Brick 1 l")
        carr = add_listing(s, "carrefour", "c1", "Leche entera Carrefour 1 L", 99, ean=MILK_EAN)
        dia = add_listing(s, "dia", "d1", "Leche entera Dia 1 L", 89, seen=now - timedelta(days=10))  # stale, similar
        lidl = add_listing(s, "lidl", "l1", "Leche entera Milbona 6 x 1 L", 510)  # not comparable size
        oil = add_listing(s, "mercadona", "m2", "Aceite de oliva 0,4º Hacendado", 1725, size="Garrafa 5 l",
                          ean="8402001027482")
        return {"milk": merc.canonical_product_id, "dia": dia.id, "oil": oil.canonical_product_id,
                "carr": carr.id, "lidl": lidl.id}  # type: ignore[dict-item]


def rows_by_chain(card: dict) -> dict[str, dict]:
    return {r["chain_id"]: r for r in card["prices"]}


def test_search_card_lists_every_chain(client: TestClient, catalog: dict[str, int]) -> None:
    r = client.get("/api/compare/search", params={"q": "leche entera"})
    assert r.status_code == 200
    body = r.json()
    assert body["postal_code"] == "28020"
    card = body["results"][0]
    assert card["product"]["ean"] == MILK_EAN
    rows = rows_by_chain(card)
    assert list(rows) == ["mercadona", "carrefour", "dia", "lidl", "alcampo", "consum"]
    assert rows["mercadona"]["match"] == "exact" and rows["mercadona"]["price_cents"] == 95
    assert rows["mercadona"]["unit_price_cents"] == 95 and rows["mercadona"]["unit"] == "l"
    assert rows["carrefour"]["match"] == "exact"
    assert rows["dia"]["match"] == "similar" and rows["dia"]["stale"] is True
    assert rows["lidl"]["match"] == "none"  # 6 L pack isn't an equivalent of 1 L
    assert rows["alcampo"]["match"] == "none" and rows["alcampo"]["price_cents"] is None
    # Cheapest by unit price: Dia 0,89 €/L.
    assert [c for c, r in rows.items() if r["cheapest"]] == ["dia"]
    assert rows["mercadona"]["can_refresh"] is True and rows["carrefour"]["can_refresh"] is False
    assert rows["mercadona"]["last_seen_at"].endswith("Z")


def test_search_by_ean(client: TestClient, catalog: dict[str, int]) -> None:
    body = client.get("/api/compare/search", params={"q": MILK_EAN}).json()
    assert len(body["results"]) == 1 and body["results"][0]["product"]["id"] == catalog["milk"]
    assert client.get("/api/compare/search", params={"q": "5449000000996"}).json()["results"] == []


def test_search_is_accent_and_case_insensitive(client: TestClient, catalog: dict[str, int]) -> None:
    body = client.get("/api/compare/search", params={"q": "ACEITE Olíva"}).json()
    assert body["results"][0]["product"]["id"] == catalog["oil"]


def test_detail_has_history_per_chain(client: TestClient, catalog: dict[str, int], clean_db: Engine) -> None:
    with Session(clean_db) as s:  # a price change at Mercadona
        add_listing(s, "mercadona", "m1", "Leche entera Hacendado", 99, ean=MILK_EAN, size="Brick 1 l",
                    seen=datetime.now(UTC) + timedelta(seconds=1))
    body = client.get(f"/api/compare/products/{catalog['milk']}").json()
    hist = {h["chain_id"]: h for h in body["history"]}
    assert [p["price_cents"] for p in hist["mercadona"]["points"]] == [95, 99]
    assert hist["dia"]["match"] == "similar"
    assert "lidl" not in hist


def test_manual_match_actions(client: TestClient, catalog: dict[str, int]) -> None:
    url = f"/api/compare/products/{catalog['milk']}/matches"
    body = client.post(url, json={"listing_id": catalog["dia"], "action": "reject"}).json()
    assert rows_by_chain(body)["dia"]["match"] == "none"

    body = client.post(url, json={"listing_id": catalog["lidl"], "action": "confirm"}).json()
    assert rows_by_chain(body)["lidl"]["match"] == "confirmed"

    body = client.post(url, json={"listing_id": catalog["dia"], "action": "relink"}).json()
    assert rows_by_chain(body)["dia"]["match"] == "exact"

    body = client.post(url, json={"listing_id": catalog["dia"], "action": "reset"}).json()
    assert rows_by_chain(body)["dia"]["match"] == "similar"

    assert client.post(url, json={"listing_id": 999, "action": "confirm"}).status_code == 404
    assert client.post(url, json={"listing_id": catalog["dia"], "action": "merge"}).status_code == 422


class FakeMercadona:
    supports_live_refresh = True
    id = "mercadona"
    user_agent = None

    def __init__(self, result: RawListing | None = None, error: Exception | None = None, delay: float = 0) -> None:
        self.result, self.error, self.delay = result, error, delay

    async def fetch_product(self, ctx, chain_product_id):  # type: ignore[no-untyped-def]
        import asyncio

        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result


def _patch_adapter(monkeypatch: pytest.MonkeyPatch, adapter: FakeMercadona) -> None:
    from app.routers import compare

    monkeypatch.setattr(compare, "build_adapter", lambda source_id: adapter if source_id == "mercadona" else None)


def _merc_listing_id(client: TestClient, product_id: int) -> int:
    body = client.get(f"/api/compare/products/{product_id}").json()
    return rows_by_chain(body)["mercadona"]["listing_id"]


def test_refresh_now_records_new_price(client: TestClient, catalog: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> None:
    raw = RawListing(chain_id="mercadona", chain_product_id="m1", name="Leche entera Hacendado", price_cents=97,
                     ean=MILK_EAN, size_text="Brick 1 l", department="food")
    _patch_adapter(monkeypatch, FakeMercadona(raw))
    r = client.post(f"/api/compare/listings/{_merc_listing_id(client, catalog['milk'])}/refresh")
    assert r.status_code == 200
    assert rows_by_chain(r.json())["mercadona"]["price_cents"] == 97


def test_refresh_now_timeout_and_errors(client: TestClient, catalog: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "refresh_timeout_seconds", 0.05)
    lid = _merc_listing_id(client, catalog["milk"])
    _patch_adapter(monkeypatch, FakeMercadona(delay=1))
    r = client.post(f"/api/compare/listings/{lid}/refresh")
    assert r.status_code == 504 and r.json()["detail"] == "refresh_timeout"

    _patch_adapter(monkeypatch, FakeMercadona(error=SourceError("blocked")))
    assert client.post(f"/api/compare/listings/{lid}/refresh").json()["detail"] == "refresh_failed"

    _patch_adapter(monkeypatch, FakeMercadona(None))
    assert client.post(f"/api/compare/listings/{lid}/refresh").status_code == 404


def test_refresh_not_supported_for_datasets(client: TestClient, catalog: dict[str, int]) -> None:
    r = client.post(f"/api/compare/listings/{catalog['carr']}/refresh")
    assert r.status_code == 409 and r.json()["detail"] == "refresh_not_supported"


def test_cheaper_elsewhere_after_scan(client: TestClient, catalog: dict[str, int], clean_db: Engine) -> None:
    stores = {s["name"]: s["id"] for s in client.get("/api/stores").json()}
    # At Carrefour, entering 0,99 €: Mercadona sells the same EAN for 0,95 € (Dia is stale -> ignored).
    alt = client.get("/api/compare/alternative", params={"barcode": MILK_EAN, "store_id": stores["Carrefour"],
                                                          "price_cents": 99}).json()["alternative"]
    assert alt["chain_id"] == "mercadona" and alt["savings_cents"] == 4 and alt["reference_label"] == "entered"

    # Without a typed price, the reference is the current chain's own listing.
    alt = client.get("/api/compare/alternative", params={"barcode": MILK_EAN, "store_id": stores["Carrefour"]}).json()
    assert alt["alternative"]["reference_label"] == "Carrefour"

    # Already the cheapest -> nothing to suggest.
    none = client.get("/api/compare/alternative", params={"barcode": MILK_EAN, "store_id": stores["Mercadona"],
                                                           "price_cents": 90}).json()
    assert none["alternative"] is None
    unknown = client.get("/api/compare/alternative", params={"barcode": "5449000000996", "price_cents": 100}).json()
    assert unknown["alternative"] is None


def test_paid_price_shows_on_card(client: TestClient, catalog: dict[str, int]) -> None:
    store_id = next(s["id"] for s in client.get("/api/stores").json() if s["name"] == "Dia")
    trip = client.post("/api/trips", json={"store_id": store_id}).json()
    client.post(f"/api/trips/{trip['id']}/purchases", json={"barcode": MILK_EAN, "unit_price": "0,91"})
    card = client.get(f"/api/compare/products/{catalog['milk']}").json()
    assert card["paid"][0]["store_name"] == "Dia" and card["paid"][0]["unit_price_cents"] == 91


def test_postal_code_setting(client: TestClient) -> None:
    assert client.get("/api/settings").json()["postal_code"] == "28020"
    assert client.put("/api/settings", json={"postal_code": "46001"}).json()["postal_code"] == "46001"
    assert client.put("/api/settings", json={"postal_code": "4600"}).status_code == 422


def test_postal_code_filters_location_specific_listings(client: TestClient, clean_db: Engine) -> None:
    with Session(clean_db) as s:
        add_listing(s, "mercadona", "m9", "Pan de molde 460 g", 120, ean="8480000999992", postal_code="46001")
    body = client.get("/api/compare/search", params={"q": "pan molde"}).json()
    assert rows_by_chain(body["results"][0])["mercadona"]["price_cents"] is None  # other postal code
    client.put("/api/settings", json={"postal_code": "46001"})
    body = client.get("/api/compare/search", params={"q": "pan molde"}).json()
    assert rows_by_chain(body["results"][0])["mercadona"]["price_cents"] == 120


def test_shopping_list_totals_missing_and_split(client: TestClient, catalog: dict[str, int], clean_db: Engine) -> None:
    with Session(clean_db) as s:
        add_listing(s, "carrefour", "c2", "Aceite de oliva 0,4º Carrefour", 1590, ean="8402001027482")
    assert client.get("/api/shopping-list").json() == {"items": [], "chains": [], "split": None}
    client.post("/api/shopping-list/items", json={"product_id": catalog["milk"], "quantity": 2})
    body = client.post("/api/shopping-list/items", json={"product_id": catalog["oil"]}).json()
    assert [(i["name"], i["quantity"]) for i in body["items"]] == [("Leche entera Hacendado", 2), ("Aceite de oliva 0,4º Hacendado", 1)]

    chains = {c["chain_id"]: c for c in body["chains"]}
    assert chains["mercadona"]["total_cents"] == 2 * 95 + 1725 and chains["mercadona"]["missing"] == 0
    assert chains["carrefour"]["total_cents"] == 2 * 99 + 1590 and chains["carrefour"]["missing"] == 0
    assert chains["dia"]["missing"] == 1 and chains["dia"]["similar"] == 1 and chains["dia"]["stale"] == 1
    assert chains["alcampo"]["missing"] == 2
    # Ranked by completeness, then total.
    assert body["chains"][0]["chain_id"] == "carrefour"

    split = body["split"]
    assert split["missing"] == 0
    # Milk at Dia (similar, 0,89) + oil at Carrefour is cheapest: 2*89 + 1590.
    assert set(split["chain_ids"]) == {"dia", "carrefour"} and split["total_cents"] == 2 * 89 + 1590

    body = client.put(f"/api/shopping-list/items/{catalog['milk']}", json={"product_id": catalog["milk"], "quantity": 1}).json()
    assert body["items"][0]["quantity"] == 1
    body = client.delete(f"/api/shopping-list/items/{catalog['oil']}").json()
    assert len(body["items"]) == 1
    assert client.post("/api/shopping-list/items", json={"product_id": 999}).status_code == 404


def test_scan_uses_catalog_name_before_open_food_facts(client: TestClient, catalog: dict[str, int], fake_off) -> None:  # type: ignore[no-untyped-def]
    body = client.get(f"/api/scan/{MILK_EAN}").json()
    assert body["found"] and body["source"] == "catalog"
    assert body["product"]["name"] == "Leche entera Hacendado"
    assert fake_off.calls == []


def test_no_cheapest_badge_with_a_single_price(client: TestClient, clean_db: Engine) -> None:
    with Session(clean_db) as s:
        add_listing(s, "mercadona", "m7", "Tomate frito Hacendado 400 g", 69, ean="8480000160164")
    card = client.get("/api/compare/search", params={"q": "tomate frito"}).json()["results"][0]
    assert not any(r["cheapest"] for r in card["prices"])


def test_no_split_when_one_store_covers_everything_cheapest(client: TestClient, clean_db: Engine) -> None:
    with Session(clean_db) as s:
        product_id = add_listing(s, "mercadona", "m1", "Arroz redondo 1 kg", 120, ean="8480000110101").canonical_product_id
        add_listing(s, "carrefour", "c1", "Arroz redondo 1 kg", 150, ean="8480000110101")
    client.post("/api/shopping-list/items", json={"product_id": product_id})
    body = client.get("/api/shopping-list").json()
    assert body["split"] is None
    assert body["chains"][0]["chain_id"] == "mercadona"
