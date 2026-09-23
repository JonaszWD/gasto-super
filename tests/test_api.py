from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.models import Purchase
from app.routers.stats import compute_stats
from app.services.openfoodfacts import OffProduct
from tests.conftest import FakeOff
from tests.test_barcode_parser import with_check_digit

COLA = "5449000000996"
HACENDADO = "8480000123459"


def start_trip(client: TestClient, store: str = "Mercadona") -> dict:
    stores = client.get("/api/stores").json()
    store_id = next(s["id"] for s in stores if s["name"] == store)
    r = client.post("/api/trips", json={"store_id": store_id})
    assert r.status_code == 201, r.text
    return r.json()


def test_health_and_static(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    r = client.get("/")
    assert r.status_code == 200
    assert "<html" in r.text


def test_default_stores_seeded_and_add_store(client: TestClient) -> None:
    names = [s["name"] for s in client.get("/api/stores").json()]
    assert names[:3] == ["Mercadona", "Carrefour", "Lidl"]
    assert "El Corte Inglés" in names
    r = client.post("/api/stores", json={"name": "  Bonpreu  "})
    assert r.status_code == 201
    assert r.json()["name"] == "Bonpreu"
    assert client.post("/api/stores", json={"name": "bonpreu"}).status_code == 409


def test_scan_off_hit_is_cached(client: TestClient, fake_off: FakeOff) -> None:
    fake_off.products[COLA] = OffProduct(COLA, "Coca-Cola (330 ml)", "Coca-Cola", "Bebidas", "https://img/x.jpg")
    r = client.get(f"/api/scan/{COLA}")
    body = r.json()
    assert r.status_code == 200
    assert body["found"] and body["source"] == "off"
    assert body["product"]["name"] == "Coca-Cola (330 ml)"
    assert body["last_unit_price_cents"] is None

    again = client.get(f"/api/scan/{COLA}").json()
    assert again["source"] == "cache"
    assert fake_off.calls == [COLA]


def test_scan_unknown_then_manual_name_is_remembered(client: TestClient, fake_off: FakeOff) -> None:
    body = client.get(f"/api/scan/{HACENDADO}").json()
    assert body["found"] is False and body["source"] == "none"

    trip = start_trip(client)
    r = client.post(
        f"/api/trips/{trip['id']}/purchases",
        json={"barcode": HACENDADO, "name": "Leche entera Hacendado", "category": "Lácteos y huevos", "unit_price": "0,95"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["unit_price_cents"] == 95

    body = client.get(f"/api/scan/{HACENDADO}").json()
    assert body["found"] and body["source"] == "cache"
    assert body["product"]["name"] == "Leche entera Hacendado"
    assert body["last_unit_price_cents"] == 95
    assert body["suggested_unit_price_cents"] == 95
    assert fake_off.calls == [HACENDADO]  # not looked up again


def test_purchase_requires_name_for_unknown_product(client: TestClient) -> None:
    trip = start_trip(client)
    r = client.post(f"/api/trips/{trip['id']}/purchases", json={"barcode": HACENDADO, "unit_price": "1"})
    assert r.status_code == 422
    assert r.json()["detail"] == "name_required"


def test_scan_off_error_flags_lookup_error(client: TestClient, fake_off: FakeOff) -> None:
    fake_off.fail = True
    body = client.get(f"/api/scan/{COLA}").json()
    assert body["found"] is False
    assert body["lookup_error"] is True


def test_scan_invalid_barcode(client: TestClient) -> None:
    assert client.get("/api/scan/123").status_code == 422


def test_scan_bad_checksum_skips_off(client: TestClient, fake_off: FakeOff) -> None:
    body = client.get("/api/scan/5449000000997").json()
    assert body["valid_checksum"] is False
    assert fake_off.calls == []


def test_variable_weight_barcode_not_sent_to_off(client: TestClient, fake_off: FakeOff) -> None:
    code = with_check_digit("211234500349")
    trip = start_trip(client)
    body = client.get(f"/api/scan/{code}", params={"store_id": trip["store_id"]}).json()
    assert fake_off.calls == []
    assert body["kind"] == "variable_price"
    assert body["embedded_price_cents"] == 349
    assert body["suggested_unit_price_cents"] == 349
    assert body["product_key"] == "vw:21:12345"
    assert body["found"] is False

    r = client.post(
        f"/api/trips/{trip['id']}/purchases",
        json={"barcode": code, "name": "Pechuga de pollo", "category": "Carne y pescado", "unit_price": "3,49"},
    )
    assert r.status_code == 201
    # Another label for the same item with a different price is recognised by name.
    other = with_check_digit("211234500512")
    body = client.get(f"/api/scan/{other}").json()
    assert body["product"]["name"] == "Pechuga de pollo"
    assert body["suggested_unit_price_cents"] == 512


def test_trip_lifecycle_and_totals(client: TestClient, fake_off: FakeOff) -> None:
    fake_off.products[COLA] = OffProduct(COLA, "Coca-Cola", None, "Bebidas", None)
    client.get(f"/api/scan/{COLA}")
    trip = start_trip(client, "Lidl")
    assert client.post("/api/trips", json={"store_id": trip["store_id"]}).status_code == 409

    tid = trip["id"]
    p1 = client.post(f"/api/trips/{tid}/purchases", json={"barcode": COLA, "unit_price": "1.25", "quantity": "2"}).json()
    client.post(f"/api/trips/{tid}/purchases", json={"barcode": COLA, "unit_price": "1,30"})

    current = client.get("/api/trips/current").json()
    assert current["id"] == tid
    assert current["store_name"] == "Lidl"
    assert current["total_cents"] == 250 + 130
    assert current["item_count"] == 2
    assert current["started_at"].endswith("Z")

    # Edit + delete items.
    r = client.patch(f"/api/purchases/{p1['id']}", json={"quantity": "3", "unit_price": "1,00"})
    assert r.json()["total_cents"] == 300
    assert client.get(f"/api/trips/{tid}").json()["total_cents"] == 430

    closed = client.post(f"/api/trips/{tid}/close").json()
    assert closed["closed_at"] is not None
    assert client.get("/api/trips/current").json() is None

    trips = client.get("/api/trips").json()
    assert trips[0]["id"] == tid and trips[0]["total_cents"] == 430

    assert client.delete(f"/api/purchases/{p1['id']}").status_code == 204
    assert client.get(f"/api/trips/{tid}").json()["total_cents"] == 130

    assert client.delete(f"/api/trips/{tid}").status_code == 204
    assert client.get(f"/api/trips/{tid}").status_code == 404


def test_rename_product_updates_purchases(client: TestClient) -> None:
    trip = start_trip(client)
    client.post(f"/api/trips/{trip['id']}/purchases", json={"barcode": HACENDADO, "name": "Leche", "unit_price": "1"})
    r = client.put(f"/api/products/{HACENDADO}", json={"name": "Leche semidesnatada", "category": "Lácteos y huevos"})
    assert r.status_code == 200
    items = client.get(f"/api/trips/{trip['id']}").json()["purchases"]
    assert items[0]["name"] == "Leche semidesnatada"
    assert items[0]["category"] == "Lácteos y huevos"


def test_invalid_price_rejected(client: TestClient) -> None:
    trip = start_trip(client)
    r = client.post(f"/api/trips/{trip['id']}/purchases", json={"barcode": HACENDADO, "name": "X", "unit_price": "abc"})
    assert r.status_code == 422


def test_price_history_stats_and_export(client: TestClient) -> None:
    trip = start_trip(client, "Dia")
    tid = trip["id"]
    for price in ("0,95", "0,99"):
        client.post(f"/api/trips/{tid}/purchases", json={"barcode": HACENDADO, "name": "Leche", "category": "Lácteos y huevos", "unit_price": price})

    hist = client.get(f"/api/stats/price-history/{HACENDADO}").json()
    assert [p["unit_price_cents"] for p in hist["points"]] == [95, 99]
    assert hist["points"][0]["store_name"] == "Dia"

    products = client.get("/api/stats/products").json()
    assert products[0]["product_key"] == HACENDADO and products[0]["purchases"] == 2

    stats = client.get("/api/stats", params={"range": "all"}).json()
    assert stats["range_total_cents"] == 194
    assert stats["by_store"] == [{"label": "Dia", "start": None, "total_cents": 194}]
    assert stats["by_category"][0]["label"] == "Lácteos y huevos"
    assert len(stats["weekly"]) == 12 and len(stats["monthly"]) == 12
    assert stats["weekly"][-1]["total_cents"] == 194

    r = client.get("/api/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    lines = r.content.decode("utf-8-sig").strip().splitlines()
    assert lines[0].startswith("fecha;hora;tienda")
    assert lines[1].split(";")[2] == "Dia"
    assert lines[1].split(";")[7] == "0,95"
    assert lines[1].split(";")[0].count("/") == 2  # DD/MM/YYYY


def test_compute_stats_uses_madrid_timezone() -> None:
    tz = ZoneInfo("Europe/Madrid")
    # 23:30 UTC on Sunday 31 Aug is 01:30 on Monday 1 Sep in Madrid.
    p = Purchase(
        trip_id=1, barcode="1", product_key="1", name="x", unit_price_cents=100, total_cents=100,
        created_at=datetime(2025, 8, 31, 23, 30, tzinfo=UTC),
    )
    stats = compute_stats([(p, "Dia")], tz, date(2025, 9, 3), "month")
    assert stats.monthly[-1].label == "2025-09" and stats.monthly[-1].total_cents == 100
    assert stats.weekly[-1].start == "2025-09-01" and stats.weekly[-1].total_cents == 100
    assert stats.range_total_cents == 100
