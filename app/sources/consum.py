"""Consum online shop (tienda.consum.es), read from its storefront REST API.

Researched 2026-10 from two open projects that use it (see README "Price sources"):
github.com/seravifer/supermarket-tracker (catalogue walk) and github.com/maurovidal23/Ray-Peat
(single product, with the x-locale/x-currency/x-zone headers the shop's own front end sends).
- GET /catalog/product?limit=100&offset=N&showRecommendations=false
      -> {"totalCount", "hasMore", "products": [...]}: the whole catalogue, 100 per page
- GET /catalog/product/code/<code>  -> one product, same shape (used for "refresh now")
- Each product has `ean`, `productData` (name, brand, format, url, imageURL), `categories` and
  `priceData.prices` ("PRICE", and "OFFER_PRICE" while discounted) with `centAmount` (the pack)
  and `centUnitAmount` (per `unitPriceUnitType`, e.g. "1 Kg"). Both are euros despite the name.
- No postal code: anonymous requests get the default zone (x-zone 0), stored with postal_code "".
- No category allowlist by id (ids weren't checked against the live shop yet): non-grocery
  categories are skipped by name, see `skip_categories` in sources.toml.
Risks: undocumented, can change without notice. About 90 page requests per full run.
"""

from collections.abc import AsyncIterator
from typing import Any

from app.services.catalog import RawListing
from app.services.quantity import parse_quantity
from app.services.text import normalize
from app.sources.base import SourceAdapter, SourceContext, SourceError, euros_to_cents
from app.sources.easycompra import guess_department

BASE = "https://tienda.consum.es/api/rest/V1.0"
HEADERS = {"x-locale": "es", "x-currency": "EUR", "x-zone": "0"}
PAGE_SIZE = 100
# A catalogue of ~9,000 products is ~90 pages; this only stops a runaway loop.
MAX_PAGES = 400

REFERENCE_UNITS = {"kg": "kg", "kilo": "kg", "l": "l", "litro": "l", "ud": "unit", "unidad": "unit", "u": "unit"}


class ConsumAdapter(SourceAdapter):
    id = "consum"
    chains = ("consum",)
    location_specific = False
    supports_live_refresh = True
    min_interval = 1.5

    def __init__(
        self,
        departments: dict[str, list[str]] | None = None,
        skip_categories: list[str] | None = None,
    ) -> None:
        # Category-name word -> department; anything unmatched falls back to the product name.
        self.department_words: dict[str, str] = {}
        for dept, words in (departments or {}).items():
            for word in words:
                self.department_words[normalize(word)] = dept
        self.skip_words = {normalize(w) for w in skip_categories or []}

    def department_for(self, product: dict[str, Any]) -> str | None:
        """None = not a grocery product (perfumery, pets...), so it isn't collected."""
        words: set[str] = set()
        for category in product.get("categories") or []:
            words.update(normalize(category.get("name") or "").split())
        if words & self.skip_words:
            return None
        for word in words:
            if word in self.department_words:
                return self.department_words[word]
        return guess_department(_name(product))

    async def fetch_catalog(self, ctx: SourceContext) -> AsyncIterator[RawListing]:
        offset = 0
        for _ in range(MAX_PAGES):
            data = await ctx.http.get_json(
                f"{BASE}/catalog/product",
                params={"limit": PAGE_SIZE, "offset": offset, "showRecommendations": "false"},
                headers=HEADERS,
            )
            products = data.get("products") or []
            for product in products:
                dept = self.department_for(product)
                if dept is None:
                    continue
                listing = parse_product(product, dept)
                if listing:
                    yield listing
            offset += len(products)
            if not products or not data.get("hasMore", offset < (data.get("totalCount") or 0)):
                return
        ctx.warnings.append(f"Consum: stopped after {MAX_PAGES} pages")

    async def fetch_product(self, ctx: SourceContext, chain_product_id: str) -> RawListing | None:
        resp = await ctx.http.request("GET", f"{BASE}/catalog/product/code/{chain_product_id}", headers=HEADERS)
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise SourceError(f"Consum product {chain_product_id}: HTTP {resp.status_code}")
        try:
            product = resp.json()
        except ValueError as exc:
            raise SourceError(f"Consum product {chain_product_id}: invalid JSON") from exc
        return parse_product(product, self.department_for(product) or "food")


def _name(product: dict[str, Any]) -> str:
    return ((product.get("productData") or {}).get("name") or "").strip()


def _current_price(price_data: dict[str, Any]) -> dict[str, Any]:
    """The price you pay today: the offer price while there is one, else the regular price."""
    prices = {p.get("id"): p.get("value") or {} for p in price_data.get("prices") or []}
    return prices.get("OFFER_PRICE") or prices.get("PRICE") or next(iter(prices.values()), {})


def _reference_unit(text: str | None) -> str | None:
    words = normalize(text or "").split()
    return REFERENCE_UNITS.get(words[-1]) if words else None


def parse_product(product: dict[str, Any], department: str) -> RawListing | None:
    data = product.get("productData") or {}
    name = _name(product)
    code = product.get("code") or product.get("id")
    price_data = product.get("priceData") or {}
    value = _current_price(price_data)
    price = euros_to_cents(value.get("centAmount"))
    if price is None or price <= 0 or not name or not code:
        return None
    size_text = (data.get("format") or "").strip() or None
    quantity = parse_quantity(size_text) or parse_quantity(name)
    unit_price = None
    ref_unit = _reference_unit(price_data.get("unitPriceUnitType"))
    if ref_unit and quantity and ref_unit == quantity.unit:
        unit_price = euros_to_cents(value.get("centUnitAmount"))
    media = product.get("media") or []
    brand = (data.get("brand") or {}).get("name") if isinstance(data.get("brand"), dict) else None
    categories = [c.get("name") for c in product.get("categories") or [] if c.get("name")]
    return RawListing(
        chain_id="consum",
        chain_product_id=str(code),
        name=name,
        brand=(brand or "").strip() or None,
        ean=(str(product["ean"]).strip() if product.get("ean") else None),
        price_cents=price,
        unit_price_cents=unit_price,
        quantity=quantity,
        size_text=size_text,
        department=department,
        category=categories[-1] if categories else None,
        image_url=data.get("imageURL") or (media[0].get("url") if media else None),
        url=data.get("url"),
    )
