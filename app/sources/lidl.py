"""Lidl Spain (lidl.es), read from the search API its own website uses.

Researched 2026-10 with a live check on a GitHub runner (the approach comes from
github.com/elopositor/EasyCompra, backend/app/lidl_scraper.py):
- GET /q/api/search?q=*&store=1&assortment=ES&locale=es_ES&version=2.1.0&fetchsize=100&offset=N
      -> {"numFound", "items": [{"gridbox": {"data": {...}}}]}. `store=1` ("En tienda") limits the
      ~5,800-item catalogue (mostly fashion, DIY and home) to the ~560 sold in shops, so a full
      run is ~10 requests. Food is `category` "Food" or "F+V" (the rest are paths like
      "Categorías/Moda/..."). The live check found 225 of the 227 food products this way; walking
      the whole catalogue found 2 more, and searching 127 grocery terms found none the others missed.
- Lidl only publishes ~220 food products online; most of its in-store range isn't there.
- No EANs (`gs1Attributes` is empty), so Lidl listings only reach other chains through "similar"
  matching. One national price for food (the per-region `zones` prices exist only on online
  non-food), stored with postal_code "".
- `price.price` is the shelf price. Some products only have a Lidl Plus price (`lidlPlus`): it is
  used and labelled ("lidl_plus"). On "2 for" offers `price.price` is the second unit's price;
  the single-unit price is taken from `basePrice` ("1 ud 0,69 €/kg / 2 uds 0,52 €/kg").
- Weekly offers carry `storeStartDate`/`storeEndDate` (epoch seconds); products not in shops yet,
  or no longer, are skipped.
- The API answers 406 to `Accept: application/json`, so `Accept: */*` is sent, with a browser
  User-Agent like the site's own requests.
Risks: undocumented, can change without notice.
"""

import re
import time
from collections.abc import AsyncIterator
from typing import Any

from app.services.catalog import RawListing
from app.services.quantity import parse_quantity
from app.sources.base import SourceAdapter, SourceContext, SourceError, euros_to_cents
from app.sources.easycompra import guess_department

BASE = "https://www.lidl.es"
API = f"{BASE}/q/api/search"
PAGE_SIZE = 100  # larger pages end early
MAX_PAGES = 50  # ~560 products sold in shops is ~6 pages; this only stops a runaway loop
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
)
HEADERS = {"Accept": "*/*", "Accept-Language": "es-ES,es;q=0.9"}
SEARCH_PARAMS = {"q": "*", "store": "1", "assortment": "ES", "locale": "es_ES", "version": "2.1.0"}

# First price in basePrice: "9,73 €/kg", or the single-unit one in "1 ud 0,69 €/kg / 2 uds 0,52 €/kg".
_BASE_PRICE = re.compile(r"(\d+(?:[.,]\d+)?)\s*€\s*/\s*(kg|l|ud)\b", re.IGNORECASE)
_MULTI_BUY = re.compile(r"^\s*1\s*(?:ud|uds|pack|packs|unidad)\b", re.IGNORECASE)
BASE_UNITS = {"kg": "kg", "l": "l", "ud": "unit"}


class LidlAdapter(SourceAdapter):
    id = "lidl"
    chains = ("lidl",)
    location_specific = False
    supports_live_refresh = False
    min_interval = 1.0
    user_agent = BROWSER_USER_AGENT

    def __init__(self, food_categories: list[str] | None = None) -> None:
        self.food_categories = set(food_categories or ["Food", "F+V"])

    async def fetch_catalog(self, ctx: SourceContext) -> AsyncIterator[RawListing]:
        now = time.time()
        offset = 0
        for _ in range(MAX_PAGES):
            resp = await ctx.http.request(
                "GET", API, params={**SEARCH_PARAMS, "fetchsize": PAGE_SIZE, "offset": offset}, headers=HEADERS
            )
            if resp.status_code >= 400:
                raise SourceError(f"Lidl search: HTTP {resp.status_code}")
            try:
                data = resp.json()
            except ValueError as exc:
                raise SourceError("Lidl search: invalid JSON") from exc
            items = data.get("items") or []
            for item in items:
                d = (item.get("gridbox") or {}).get("data") or {}
                if d.get("category") not in self.food_categories or not in_shops(d, now):
                    continue
                listing = parse_product(d)
                if listing:
                    yield listing
            offset += len(items)
            if not items or offset >= (data.get("numFound") or 0):
                return
        ctx.warnings.append(f"Lidl: stopped after {MAX_PAGES} pages")


def in_shops(d: dict[str, Any], now: float) -> bool:
    """False for weekly offers that haven't reached the shops yet, or have left them."""
    start, end = d.get("storeStartDate"), d.get("storeEndDate")
    if isinstance(start, (int, float)) and start > now:
        return False
    return not (isinstance(end, (int, float)) and end < now)


def _base_price(text: str | None) -> tuple[str, int] | None:
    """Lidl's own unit price as (base unit, cents per kg / l / unit), or None."""
    m = _BASE_PRICE.search(text or "")
    if not m:
        return None
    cents = euros_to_cents(m.group(1).replace(",", "."))
    return (BASE_UNITS[m.group(2).lower()], cents) if cents else None


def parse_product(d: dict[str, Any]) -> RawListing | None:
    name = (d.get("title") or d.get("fullTitle") or "").strip().lstrip("- ").strip()
    product_id = d.get("productId") or d.get("erpNumber")
    price_data = d.get("price") or {}
    price, label = price_data.get("price"), None
    if price is None:
        # Some products are only priced with the Lidl Plus app.
        price_data = next(((o.get("price") or {}) for o in d.get("lidlPlus") or [] if (o.get("price") or {}).get("price") is not None), {})
        price, label = price_data.get("price"), "lidl_plus"
    price_cents = euros_to_cents(price)
    if not name or not product_id or not price_cents or price_cents <= 0:
        return None
    size_text = ((price_data.get("packaging") or {}).get("text") or "").strip() or None
    quantity = parse_quantity(size_text) or parse_quantity(name)
    base_text = (price_data.get("basePrice") or {}).get("text") if isinstance(price_data.get("basePrice"), dict) else None
    unit_price = None
    base = _base_price(base_text)
    if base and quantity and base[0] == quantity.unit:
        unit_price = base[1]
        if _MULTI_BUY.search(base_text or ""):
            # "2 for" offer: price.price is the second unit's; one unit costs unit price x size.
            price_cents = round(unit_price * quantity.value)
    brand = (d.get("brand") or {}).get("name")
    brand = brand.strip() if brand and brand.strip() not in ("", "-") else None
    category = ((d.get("keyfacts") or {}).get("wonCategoryPrimary") or "").split("/")[-1].strip() or d.get("category")
    return RawListing(
        chain_id="lidl",
        chain_product_id=str(product_id),
        name=name,
        brand=brand,
        price_cents=price_cents,
        unit_price_cents=unit_price,
        quantity=quantity,
        size_text=size_text,
        department=guess_department(name),
        category=category,
        image_url=d.get("image"),
        url=f"{BASE}{d['canonicalUrl']}" if d.get("canonicalUrl") else None,
        price_label=label,
    )
