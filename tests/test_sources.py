"""Source adapters against recorded responses (tests/fixtures), never the real stores."""

import httpx
import pytest

from app.sources.base import SourceError
from app.sources.easycompra import EasyCompraAdapter, guess_department
from app.sources.mercadona import MercadonaAdapter
from app.sources.openprices import OpenPricesAdapter, chain_for
from app.sources.registry import build_adapter, load_config
from tests.sources_helpers import fixture, make_ctx


def mercadona() -> MercadonaAdapter:
    adapter = build_adapter("mercadona", load_config())
    assert isinstance(adapter, MercadonaAdapter)
    return adapter


def mercadona_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/postal-codes/actions/change-pc/"):
        return httpx.Response(200, headers={"x-customer-wh": "mad3"})
    if path == "/api/categories/":
        return httpx.Response(200, json=fixture("mercadona/categories.json"))
    if path == "/api/categories/112/":
        return httpx.Response(200, json=fixture("mercadona/category_112.json"))
    if path == "/api/categories/229/":
        return httpx.Response(200, json={"id": 229, "categories": []})
    if path == "/api/products/4241/":
        return httpx.Response(200, json=fixture("mercadona/product_4241.json"))
    return httpx.Response(404, json={})


async def test_mercadona_catalog_uses_postal_code_warehouse_and_category_allowlist() -> None:
    ctx, seen = make_ctx(mercadona_handler)
    listings = [x async for x in mercadona().fetch_catalog(ctx)]

    change_pc = seen[0]
    assert change_pc.method == "PUT" and b"28020" in change_pc.content
    assert all(r.url.params.get("wh") == "mad3" for r in seen[1:])
    paths = [r.url.path for r in seen]
    assert "/api/categories/185/" not in paths  # facial care: not food/drink/household
    assert "/api/categories/229/" in paths  # household cleaning

    assert [x.chain_product_id for x in listings] == ["4241", "4240", "4717"]
    big = listings[0]
    assert big.name == "Aceite de oliva 0,4º Hacendado"
    assert big.price_cents == 1725
    assert big.unit_price_cents == 345  # 3,45 €/L from reference_price
    assert big.quantity and big.quantity.value == 5.0 and big.quantity.unit == "l"
    assert big.department == "food"
    assert big.ean is None  # the listing endpoint has no EAN
    assert big.size_text == "Garrafa 5 l"


async def test_mercadona_warehouse_is_cached() -> None:
    ctx, seen = make_ctx(mercadona_handler)
    adapter = mercadona()
    assert await adapter.warehouse(ctx) == "mad3"
    assert await adapter.warehouse(ctx) == "mad3"
    assert sum(r.method == "PUT" for r in seen) == 1


async def test_mercadona_product_has_ean() -> None:
    ctx, _ = make_ctx(mercadona_handler)
    listing = await mercadona().fetch_product(ctx, "4241")
    assert listing is not None
    assert listing.ean == "8402001027482"
    assert listing.brand == "Hacendado"
    assert listing.price_cents == 1725


async def test_mercadona_unknown_product_returns_none() -> None:
    ctx, _ = make_ctx(mercadona_handler)
    assert await mercadona().fetch_product(ctx, "999999") is None


async def test_mercadona_retries_on_429_then_fails_cleanly() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(200, headers={"x-customer-wh": "mad3"})
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "1"})

    ctx, _ = make_ctx(handler)
    with pytest.raises(SourceError):
        [x async for x in mercadona().fetch_catalog(ctx)]
    assert calls["n"] == 3  # first try + 2 retries


async def test_mercadona_search_not_supported() -> None:
    ctx, _ = make_ctx(mercadona_handler)
    with pytest.raises(SourceError):
        await mercadona().search(ctx, "leche")


def easycompra_handler(request: httpx.Request) -> httpx.Response:
    name = request.url.path.rsplit("/", 1)[-1]
    try:
        return httpx.Response(200, json=fixture(f"easycompra/{name}"))
    except FileNotFoundError:
        return httpx.Response(404)


async def test_easycompra_parses_and_skips_stale_chains() -> None:
    ctx, seen = make_ctx(easycompra_handler, chains={"carrefour", "dia"})
    listings = [x async for x in EasyCompraAdapter().fetch_catalog(ctx)]
    assert {x.chain_id for x in listings} == {"carrefour"}
    assert not any(r.url.path.endswith("/dia.json") for r in seen)
    assert any("dia" in w for w in ctx.warnings)

    yogur = listings[0]
    assert yogur.ean == "3560071246136"
    assert yogur.price_cents == 179
    assert yogur.quantity and (yogur.quantity.value, yogur.quantity.unit) == (1.0, "kg")
    assert yogur.resolved_unit_price() == 179
    actimel = listings[1]
    assert actimel.quantity and actimel.quantity.value == pytest.approx(0.6)  # 6 x 100 g
    assert actimel.observed_at is not None and actimel.observed_at.year == 2026


async def test_easycompra_search_filters_by_words() -> None:
    ctx, _ = make_ctx(easycompra_handler, chains={"carrefour"})
    found = await EasyCompraAdapter().search(ctx, "griego vainilla")
    assert [x.chain_product_id for x in found] == ["VC4AECOMM-628237"]


def test_guess_department() -> None:
    assert guess_department("Agua mineral natural 1,5 L") == "drink"
    assert guess_department("Leche entera 1 L") == "food"
    assert guess_department("Detergente líquido 30 lavados") == "household"


def openprices_handler(request: httpx.Request) -> httpx.Response:
    if "nominatim" in request.url.host:
        return httpx.Response(200, json=[{"lat": "40.4565", "lon": "-3.6953"}])
    return httpx.Response(200, json=fixture("openprices/prices.json"))


async def test_openprices_maps_osm_brands_to_chains_and_sorts_by_date() -> None:
    ctx, seen = make_ctx(openprices_handler)
    listings = [x async for x in OpenPricesAdapter().fetch_catalog(ctx)]
    prices_req = next(r for r in seen if "prices" in r.url.path)
    assert prices_req.url.params["lat"] == "40.4565" and prices_req.url.params["radius_km"] == "10"
    # Covirán isn't a tracked chain -> dropped. DIA&GO / "Supermercados Dia" -> dia.
    assert [(x.chain_id, x.ean) for x in listings] == [
        ("dia", "8480017240804"),
        ("dia", "5415191700106"),
        ("mercadona", "8480000056733"),
    ]
    dates = [x.observed_at for x in listings]
    assert dates == sorted(dates)
    milk = listings[0]
    assert milk.price_cents == 109 and milk.quantity and milk.quantity.unit == "l"


async def test_openprices_geocode_is_cached() -> None:
    ctx, seen = make_ctx(openprices_handler)
    adapter = OpenPricesAdapter()
    await adapter.coordinates(ctx)
    await adapter.coordinates(ctx)
    assert sum("nominatim" in r.url.host for r in seen) == 1


def test_chain_for() -> None:
    assert chain_for({"osm_brand": "Mercadona"}) == "mercadona"
    assert chain_for({"osm_name": "DIA&GO"}) == "dia"
    assert chain_for({"osm_brand": "Hipercor"}) == "el-corte-ingles"
    assert chain_for({"osm_brand": "Diagonal Market"}) is None
    assert chain_for(None) is None
