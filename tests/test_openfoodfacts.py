import httpx
import pytest
import respx

from app.config import Settings
from app.services.openfoodfacts import OffLookupError, OpenFoodFactsClient, map_category, parse_product

BASE = "https://off.test"
SETTINGS = Settings(off_base_url=BASE, off_user_agent="TestAgent/1.0 (test@example.com)")


def off_payload(**product: object) -> dict:
    return {"status": 1, "code": "5449000000996", "product": product}


async def test_lookup_found_sends_user_agent() -> None:
    with respx.mock:
        route = respx.get(f"{BASE}/api/v2/product/5449000000996.json").mock(
            return_value=httpx.Response(
                200,
                json=off_payload(
                    product_name="Coca-Cola",
                    product_name_es="Coca-Cola Original",
                    brands="Coca-Cola, The Coca-Cola Company",
                    quantity="330 ml",
                    categories_tags=["en:beverages", "en:carbonated-drinks", "en:sodas"],
                    image_front_small_url="https://img.test/cola.jpg",
                ),
            )
        )
        product = await OpenFoodFactsClient(SETTINGS).lookup("5449000000996")
    assert route.called
    request = route.calls.last.request
    assert request.headers["User-Agent"] == "TestAgent/1.0 (test@example.com)"
    assert "fields=" in str(request.url)
    assert product is not None
    assert product.name == "Coca-Cola Original (330 ml)"
    assert product.brand == "Coca-Cola"
    assert product.category == "Bebidas"
    assert product.image_url == "https://img.test/cola.jpg"


async def test_lookup_not_found() -> None:
    with respx.mock:
        respx.get(f"{BASE}/api/v2/product/8480000000000.json").mock(
            return_value=httpx.Response(404, json={"status": 0, "status_verbose": "product not found"})
        )
        assert await OpenFoodFactsClient(SETTINGS).lookup("8480000000000") is None


async def test_lookup_status_zero_with_200() -> None:
    with respx.mock:
        respx.get(f"{BASE}/api/v2/product/1.json").mock(return_value=httpx.Response(200, json={"status": 0}))
        assert await OpenFoodFactsClient(SETTINGS).lookup("1") is None


async def test_lookup_network_error_raises() -> None:
    with respx.mock:
        respx.get(f"{BASE}/api/v2/product/1.json").mock(side_effect=httpx.ConnectTimeout("boom"))
        with pytest.raises(OffLookupError):
            await OpenFoodFactsClient(SETTINGS).lookup("1")


async def test_lookup_server_error_raises() -> None:
    with respx.mock:
        respx.get(f"{BASE}/api/v2/product/1.json").mock(return_value=httpx.Response(503))
        with pytest.raises(OffLookupError):
            await OpenFoodFactsClient(SETTINGS).lookup("1")


def test_parse_product_without_name_is_none() -> None:
    assert parse_product("1", off_payload(product_name="  ")) is None


def test_map_category_prefers_specific_tags() -> None:
    assert map_category(["en:plant-based-foods", "en:fruits"]) == "Fruta y verdura"
    assert map_category(["en:dairies", "en:yogurts"]) == "Lácteos y huevos"
    assert map_category(["en:something-else"]) is None
    assert map_category(None) is None
