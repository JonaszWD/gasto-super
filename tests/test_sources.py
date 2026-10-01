"""Source adapters against recorded responses (tests/fixtures), never the real stores."""

import json

import httpx
import pytest

from app.sources.alcampo import AlcampoAdapter, parse_entity as parse_alcampo_entity
from app.sources.base import SourceError
from app.sources.consum import ConsumAdapter
from app.sources.easycompra import EasyCompraAdapter, guess_department
from app.sources.mercadona import MercadonaAdapter
from app.sources.openprices import OpenPricesAdapter, chain_for
from app.sources.registry import build_adapter, load_config
from tests.sources_helpers import FIXTURES, fixture, make_ctx


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


def alcampo() -> AlcampoAdapter:
    adapter = build_adapter("alcampo", load_config())
    assert isinstance(adapter, AlcampoAdapter)
    return adapter


def alcampo_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/search" and request.url.params.get("q") == "leche":
        return httpx.Response(200, text=(FIXTURES / "alcampo/search_leche.html").read_text(encoding="utf-8"))
    if path == "/search":
        return httpx.Response(200, text="<html><body>no state here</body></html>")
    if path == "/products/54180":
        return httpx.Response(200, text=(FIXTURES / "alcampo/product_54180.html").read_text(encoding="utf-8"))
    return httpx.Response(404, text="")


async def test_alcampo_search_parses_page_state_and_skips_non_food_and_unpriced() -> None:
    ctx, seen = make_ctx(alcampo_handler)
    found = await alcampo().search(ctx, "leche")
    assert seen[0].headers["accept"].startswith("text/html")
    # 999001 is under Perfumería (not collected), 999002 has an empty price.
    assert [x.chain_product_id for x in found] == ["54180", "54178", "53549", "99193"]
    pack = found[0]
    assert pack.chain_id == "alcampo" and pack.ean is None
    assert pack.name == "AUCHAN Leche semidesnatada de vaca 6 x 1l Producto Alcampo."
    assert pack.price_cents == 528 and pack.unit_price_cents == 88
    assert pack.quantity and (pack.quantity.value, pack.quantity.unit) == (6.0, "l")
    assert pack.department == "food" and pack.category == "Leche semidesnatada"
    assert pack.url == "https://www.compraonline.alcampo.es/products/54180"


async def test_alcampo_catalog_walks_search_terms_and_dedupes() -> None:
    ctx, seen = make_ctx(alcampo_handler)
    adapter = AlcampoAdapter({"food": ["Leche, Huevos, Lácteos, Yogures y Bebidas vegetales"]}, ["leche", "pan", "leche"])
    listings = [x async for x in adapter.fetch_catalog(ctx)]
    assert [r.url.params["q"] for r in seen] == ["leche", "pan", "leche"]
    assert len(listings) == 4 and len({x.chain_product_id for x in listings}) == 4


async def test_alcampo_catalog_keeps_going_after_a_failed_search() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("q") == "pan":
            return httpx.Response(403, text="")
        return alcampo_handler(request)

    ctx, _ = make_ctx(handler)
    adapter = AlcampoAdapter({"food": ["Leche, Huevos, Lácteos, Yogures y Bebidas vegetales"]}, ["pan", "leche"])
    listings = [x async for x in adapter.fetch_catalog(ctx)]
    assert len(listings) == 4
    assert any("pan" in w and "403" in w for w in ctx.warnings)


async def test_alcampo_product_page() -> None:
    ctx, _ = make_ctx(alcampo_handler)
    product = await alcampo().fetch_product(ctx, "54180")
    assert product is not None
    assert product.price_cents == 528 and product.unit_price_cents == 88
    assert product.size_text == "6000ml" and product.department == "food"
    assert await alcampo().fetch_product(ctx, "1") is None


def test_alcampo_sends_a_browser_user_agent() -> None:
    assert "Mozilla/5.0" in (alcampo().user_agent or "")


def test_alcampo_pack_in_name_beats_a_wrong_size_field() -> None:
    entity = {
        "retailerProductId": "201028",
        "name": "PULEVA Tido Leche entera 6 x 200 ml.",
        "categoryPath": ["Leche, Huevos, Lácteos, Yogures y Bebidas vegetales", "Leche", "Leche entera"],
        "price": {
            "current": {"amount": "2.57", "currency": "EUR"},
            "unit": {"label": "fop.price.per.litre", "current": {"amount": "4.28", "currency": "EUR"}},
        },
        "size": {"value": "600ml"},  # as served by Alcampo; the pack is 1,2 l
    }
    listing = parse_alcampo_entity(entity, "food")
    assert listing and listing.quantity and (listing.quantity.value, listing.quantity.unit) == (1.2, "l")
    assert listing.unit_price_cents is None and listing.resolved_unit_price() == 214

    entity["size"] = {"value": "1200ml"}  # consistent size: Alcampo's unit price is kept
    entity["price"]["unit"]["current"]["amount"] = "2.14"
    listing = parse_alcampo_entity(entity, "food")
    assert listing and listing.unit_price_cents == 214


async def test_alcampo_catalog_stops_after_three_refusals_in_a_row() -> None:
    ctx, seen = make_ctx(lambda r: httpx.Response(403, text=""))
    adapter = AlcampoAdapter({"food": ["Leche, Huevos, Lácteos, Yogures y Bebidas vegetales"]}, ["a", "b", "c", "d", "e"])
    with pytest.raises(SourceError, match="blocking"):
        [x async for x in adapter.fetch_catalog(ctx)]
    assert len(seen) == 3  # "d" and "e" are never requested


async def test_alcampo_refusal_count_resets_after_a_good_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("q") == "leche":
            return alcampo_handler(request)
        return httpx.Response(429, text="")

    ctx, seen = make_ctx(handler)
    adapter = AlcampoAdapter(
        {"food": ["Leche, Huevos, Lácteos, Yogures y Bebidas vegetales"]}, ["a", "b", "leche", "c", "d"]
    )
    listings = [x async for x in adapter.fetch_catalog(ctx)]
    assert len(listings) == 4
    assert {r.url.params["q"] for r in seen} == {"a", "b", "leche", "c", "d"}  # 429s are retried, never fatal here


def alcampo_page(term: str, count: int) -> str:
    """A search page with `count` food products whose ids start with the term."""
    entities = {
        f"uuid-{term}-{i}": {
            "retailerProductId": f"{term}{i}",
            "name": f"Producto {term} {i} 1 l",
            "categoryPath": ["Alimentación"],
            "price": {"current": {"amount": "1.00", "currency": "EUR"}},
        }
        for i in range(count)
    }
    return '<script>window.__INITIAL_STATE__={"data":{"products":{"productEntities":%s}}};</script>' % json.dumps(entities)


async def test_alcampo_capped_runs_rotate_through_the_terms() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=alcampo_page(request.url.params["q"], 4))

    adapter = AlcampoAdapter({"food": ["Alimentación"]}, ["a", "b"], max_products=3)
    ctx, seen = make_ctx(handler)
    nights = []
    for _ in range(3):
        nights.append([x.chain_product_id async for x in adapter.fetch_catalog(ctx)])  # same cache: next night
    assert nights == [["a0", "a1", "a2"], ["a3", "b0", "b1"], ["b2", "b3", "a0"]]
    assert [r.url.params["q"] for r in seen] == ["a", "a", "b", "b", "a"]
    assert ctx.cache.get("alcampo:cursor") == "0:1"


async def test_alcampo_blocked_run_does_not_move_the_cursor() -> None:
    adapter = AlcampoAdapter({"food": ["Alimentación"]}, ["a", "b", "c"], max_products=3)
    ctx, _ = make_ctx(lambda r: httpx.Response(403, text=""))
    ctx.cache.set("alcampo:cursor", "1:2")
    with pytest.raises(SourceError):
        [x async for x in adapter.fetch_catalog(ctx)]
    assert ctx.cache.get("alcampo:cursor") == "1:2"


def test_alcampo_config_caps_nightly_products() -> None:
    assert alcampo().max_products == 50


# Consum: the fixtures follow the response shape documented by github.com/seravifer/supermarket-tracker
# (src/consum/types.ts); the live shop couldn't be reached when they were written.


def consum() -> ConsumAdapter:
    adapter = build_adapter("consum", load_config())
    assert isinstance(adapter, ConsumAdapter)
    return adapter


def consum_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/rest/V1.0/catalog/product":
        name = f"consum/catalog_{request.url.params['offset']}.json"
        if (FIXTURES / name).exists():
            return httpx.Response(200, json=fixture(name))
        return httpx.Response(200, json={"totalCount": 7, "hasMore": False, "products": []})
    if path == "/api/rest/V1.0/catalog/product/code/7062185":
        return httpx.Response(200, json=fixture("consum/product_7062185.json"))
    return httpx.Response(404, json={})


async def test_consum_catalog_pages_and_skips_non_grocery_and_unpriced() -> None:
    ctx, seen = make_ctx(consum_handler)
    listings = [x async for x in consum().fetch_catalog(ctx)]

    assert [r.url.params["offset"] for r in seen] == ["0", "4"]  # stops when hasMore is false
    assert all(r.url.params["limit"] == "100" and r.headers["x-locale"] == "es" for r in seen)
    by_id = {x.chain_product_id: x for x in listings}
    assert set(by_id) == {"7062185", "7148261", "7001234", "7155555", "7177777"}  # no shampoo, no unpriced item

    milk = by_id["7062185"]
    assert milk.chain_id == "consum" and milk.ean == "8480000123459"
    assert milk.name == "Leche Entera" and milk.brand == "Consum"
    assert milk.price_cents == 95 and milk.unit_price_cents == 95
    assert milk.quantity and milk.quantity.value == 1.0 and milk.quantity.unit == "l"
    assert milk.department == "food" and milk.category == "Leche"
    assert milk.url == "https://tienda.consum.es/es/p/leche-entera/7062185"

    oil = by_id["7148261"]
    assert oil.price_cents == 749  # the offer price, not the regular 8,95
    assert by_id["7001234"].department == "drink"  # "Aguas" category
    assert by_id["7001234"].unit_price_cents == 18  # 6 x 1,5 l at 0,18 €/L
    detergent = by_id["7155555"]
    assert detergent.department == "household"
    assert detergent.unit_price_cents is None  # "per wash" isn't a unit we compare by
    banana = by_id["7177777"]
    assert banana.brand is None and banana.ean is None
    assert banana.image_url and banana.image_url.endswith("1000x1000/7177777_001.jpg")  # media fallback


async def test_consum_refresh_reads_one_product() -> None:
    ctx, _ = make_ctx(consum_handler)
    adapter = consum()
    listing = await adapter.fetch_product(ctx, "7062185")
    assert listing is not None and listing.price_cents == 99 and listing.department == "food"
    assert await adapter.fetch_product(ctx, "1") is None


async def test_consum_blocked_catalog_raises() -> None:
    ctx, _ = make_ctx(lambda r: httpx.Response(403, text=""))
    with pytest.raises(SourceError):
        [x async for x in consum().fetch_catalog(ctx)]
