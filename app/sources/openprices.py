"""Open Food Facts "Open Prices" (prices.openfoodfacts.org), a public, crowd-sourced price database.

Read API, no auth. Filtered by distance around the postal code (lat/lon/radius_km). Coverage in
Spain is sparse (a handful of prices within 5 km of 28020 in 2026-09), so this supplements the
other sources. Each price carries its own date and the shop's OpenStreetMap brand, which we map
to a chain. The postal code is geocoded once with OpenStreetMap Nominatim and cached.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from app.services.catalog import RawListing
from app.services.quantity import Quantity, parse_quantity
from app.services.text import normalize
from app.sources.base import SourceAdapter, SourceContext, SourceError, euros_to_cents

BASE = "https://prices.openfoodfacts.org/api/v1"
NOMINATIM = "https://nominatim.openstreetmap.org/search"

# Normalized OSM brand/name prefix -> chain id.
BRANDS: list[tuple[str, str]] = [
    ("mercadona", "mercadona"),
    ("supermercados dia", "dia"),
    ("dia", "dia"),
    ("carrefour", "carrefour"),
    ("lidl", "lidl"),
    ("alcampo", "alcampo"),
    ("auchan", "alcampo"),
    ("aldi", "aldi"),
    ("eroski", "eroski"),
    ("consum", "consum"),
    ("ahorramas", "ahorramas"),
    ("el corte ingles", "el-corte-ingles"),
    ("hipercor", "el-corte-ingles"),
    ("supercor", "el-corte-ingles"),
]


def chain_for(location: dict[str, Any] | None) -> str | None:
    if not location:
        return None
    for field in ("osm_brand", "osm_name"):
        value = normalize(location.get(field))
        for prefix, chain in BRANDS:
            if value == prefix or value.startswith(prefix + " "):
                return chain
    return None


def department_for(product: dict[str, Any] | None) -> str:
    tags = " ".join((product or {}).get("categories_tags") or [])
    if "beverages" in tags and "dairies" not in tags:
        return "drink"
    if "household" in tags or "cleaning" in tags:
        return "household"
    return "food"


class OpenPricesAdapter(SourceAdapter):
    id = "openprices"
    chains = tuple(sorted({c for _, c in BRANDS}))
    location_specific = True
    supports_live_refresh = False
    min_interval = 1.0

    def __init__(self, radius_km: float = 10, days: int = 120, max_pages: int = 20) -> None:
        self.radius_km = radius_km
        self.days = days
        self.max_pages = max_pages

    async def coordinates(self, ctx: SourceContext) -> tuple[float, float]:
        key = f"geo:{ctx.postal_code}"
        cached = ctx.cache.get(key)
        if cached:
            lat, lon = cached.split(",")
            return float(lat), float(lon)
        results = await ctx.http.get_json(
            NOMINATIM, params={"postalcode": ctx.postal_code, "country": "es", "format": "json", "limit": 1}
        )
        if not results:
            raise SourceError(f"Could not geocode postal code {ctx.postal_code}")
        lat, lon = float(results[0]["lat"]), float(results[0]["lon"])
        ctx.cache.set(key, f"{lat},{lon}")
        return lat, lon

    async def fetch_catalog(self, ctx: SourceContext) -> AsyncIterator[RawListing]:
        lat, lon = await self.coordinates(ctx)
        since = (datetime.now(UTC) - timedelta(days=self.days)).date().isoformat()
        items: list[dict[str, Any]] = []
        for page in range(1, self.max_pages + 1):
            data = await ctx.http.get_json(
                f"{BASE}/prices",
                params={
                    "lat": lat, "lon": lon, "radius_km": self.radius_km, "date__gte": since,
                    "product_code__isnull": "false", "order_by": "date", "size": 100, "page": page,
                },
            )
            items.extend(data.get("items") or [])
            if page >= (data.get("pages") or 1):
                break
        # Oldest first, so the price history is built in time order.
        for item in sorted(items, key=lambda i: (i.get("date") or "", i.get("id") or 0)):
            listing = parse_price(item)
            if listing and self.wants_chain(ctx, listing.chain_id):
                yield listing


def parse_price(item: dict[str, Any]) -> RawListing | None:
    if item.get("currency") not in (None, "EUR") or not item.get("product_code"):
        return None
    chain = chain_for(item.get("location"))
    price = euros_to_cents(item.get("price"))
    if chain is None or price is None:
        return None
    product = item.get("product") or {}
    name = product.get("product_name") or item.get("product_name") or item["product_code"]
    size_text = " ".join(str(x) for x in (product.get("product_quantity"), product.get("product_quantity_unit")) if x)
    quantity: Quantity | None = parse_quantity(size_text) or parse_quantity(name)
    unit_price = None
    if item.get("price_per") == "KILOGRAM":
        quantity, unit_price = Quantity(1.0, "kg"), price
    date = item.get("date")
    observed = datetime.fromisoformat(date).replace(tzinfo=UTC) if date else None
    return RawListing(
        chain_id=chain,
        chain_product_id=str(item["product_code"]),
        name=str(name)[:200],
        brand=((product.get("brands") or "").split(",")[0].strip() or None),
        ean=str(item["product_code"]),
        price_cents=price,
        unit_price_cents=unit_price,
        quantity=quantity,
        size_text=size_text or None,
        department=department_for(product),
        image_url=product.get("image_url"),
        observed_at=observed,
    )
