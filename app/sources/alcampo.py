"""Alcampo online shop (compraonline.alcampo.es), read from its server-rendered pages.

Researched 2026-09 (see README "Price sources"), based on github.com/jgalea/grocery-cli:
- The shop's JSON API returns HTTP 403 to non-browser clients, but the HTML pages answer when
  the request carries a browser User-Agent. That is the only thing this adapter changes: no
  cookies, no JS challenges, no proxies. If Alcampo adds real bot protection, it stops working.
- GET /search?q=<term>        -> `window.__INITIAL_STATE__` has "productEntities" (first 50 hits)
- GET /products/<id>          -> redirects to the slug URL; `window.__QUERY_INITIAL_STATE__`
                                 has the product (used for "refresh now")
- No EANs anywhere, so Alcampo listings only reach other chains through "similar" matching.
- There's no postal code: prices are the shop's default region (stored with postal_code "").
- The catalogue is walked by searching the terms in sources.toml, not the category tree;
  products outside those terms are not collected.
Risks: undocumented page state, can change without notice. Pages are 1-2 MB each, so we keep
to one request every ~2 s.
"""

import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

from app.services.catalog import RawListing
from app.services.quantity import pack_quantity, parse_quantity
from app.services.text import normalize
from app.sources.base import SourceAdapter, SourceContext, SourceError, euros_to_cents

BASE = "https://www.compraonline.alcampo.es"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
)
HTML_HEADERS = {"Accept": "text/html,application/xhtml+xml", "Accept-Language": "es-ES,es;q=0.9"}
# Consecutive 403/429 answers after which a catalogue run gives up.
MAX_BLOCKED_IN_A_ROW = 3
# Where the last capped run stopped: "<term index>:<products to skip in that term's results>".
CURSOR_KEY = "alcampo:cursor"


class AlcampoBlocked(SourceError):
    """Alcampo refused the request (HTTP 403 or 429)."""


class AlcampoAdapter(SourceAdapter):
    id = "alcampo"
    chains = ("alcampo",)
    location_specific = False
    supports_live_refresh = True
    min_interval = 2.0
    user_agent = BROWSER_USER_AGENT

    def __init__(
        self,
        departments: dict[str, list[str]] | None = None,
        search_terms: list[str] | None = None,
        max_products: int | None = None,
    ) -> None:
        # normalized top-level category name -> department; anything else is skipped.
        self.departments: dict[str, str] = {}
        for dept, names in (departments or {}).items():
            for name in names:
                self.departments[normalize(name)] = dept
        self.search_terms = list(search_terms or [])
        self.max_products = max_products or None  # per catalogue run; None = every term

    def department_for(self, category_path: list[str] | None) -> str | None:
        if not category_path:
            return None
        return self.departments.get(normalize(category_path[0]))

    async def _page(self, ctx: SourceContext, url: str) -> str | None:
        resp = await ctx.http.request("GET", url, headers=HTML_HEADERS)
        if resp.status_code == 404:
            return None
        if resp.status_code in (403, 429):
            raise AlcampoBlocked(f"Alcampo {url}: HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise SourceError(f"Alcampo {url}: HTTP {resp.status_code}")
        return resp.text

    async def _search_page(self, ctx: SourceContext, term: str) -> list[RawListing]:
        html = await self._page(ctx, f"{BASE}/search?q={quote(term)}")
        out = []
        for entity in product_entities(html or ""):
            listing = parse_entity(entity, self.department_for(entity.get("categoryPath")))
            if listing:
                out.append(listing)
        return out

    async def fetch_catalog(self, ctx: SourceContext) -> AsyncIterator[RawListing]:
        """Walk the search terms. With max_products set, each run takes the next that many products
        and remembers where it stopped (term + position, in appsetting), so successive nights
        rotate through the whole term list instead of re-reading the first terms."""
        terms = self.search_terms
        if not terms:
            return
        start, skip = self._cursor(ctx, len(terms)) if self.max_products else (0, 0)
        seen: set[str] = set()
        yielded = 0
        blocked_in_a_row = 0
        for step in range(len(terms)):
            index = (start + step) % len(terms)
            term = terms[index]
            try:
                found = await self._search_page(ctx, term)
            except AlcampoBlocked as exc:
                blocked_in_a_row += 1
                if blocked_in_a_row >= MAX_BLOCKED_IN_A_ROW:
                    # Stop instead of sending the rest of the run to a site that is refusing us.
                    raise SourceError(
                        f"Alcampo is blocking requests ({blocked_in_a_row} refused in a row, last: {exc}); run stopped"
                    ) from exc
                ctx.warnings.append(f"Alcampo: search {term!r} refused ({exc})")
                continue
            except SourceError as exc:
                blocked_in_a_row = 0
                ctx.warnings.append(f"Alcampo: search {term!r} failed ({exc})")
                continue
            blocked_in_a_row = 0
            first = skip if step == 0 else 0
            for position in range(first, len(found)):
                listing = found[position]
                if listing.chain_product_id in seen:
                    continue
                seen.add(listing.chain_product_id)
                yielded += 1
                if self.max_products and yielded >= self.max_products:
                    # Saved before the last yield: the runner may close us right after it.
                    if position + 1 < len(found):
                        self._save_cursor(ctx, index, position + 1)
                    else:
                        self._save_cursor(ctx, (index + 1) % len(terms), 0)
                    yield listing
                    return
                yield listing
        if self.max_products:
            self._save_cursor(ctx, start, 0)  # the whole list fitted in one run

    def _cursor(self, ctx: SourceContext, n_terms: int) -> tuple[int, int]:
        try:
            index, skip = (int(x) for x in (ctx.cache.get(CURSOR_KEY) or "0:0").split(":"))
        except ValueError:
            return 0, 0
        return (index, skip) if 0 <= index < n_terms and skip >= 0 else (0, 0)

    def _save_cursor(self, ctx: SourceContext, index: int, skip: int) -> None:
        ctx.cache.set(CURSOR_KEY, f"{index}:{skip}")

    async def search(self, ctx: SourceContext, query: str) -> list[RawListing]:
        return await self._search_page(ctx, query)

    async def fetch_product(self, ctx: SourceContext, chain_product_id: str) -> RawListing | None:
        html = await self._page(ctx, f"{BASE}/products/{quote(chain_product_id)}")
        product = page_product(html or "", chain_product_id)
        if product is None:
            return None
        # The product page has a flatter shape than the search entities.
        entity = {
            **product,
            "price": {
                "current": product.get("price") or {},
                "unit": {
                    "label": (product.get("unitPrice") or {}).get("unit"),
                    "current": (product.get("unitPrice") or {}).get("price") or {},
                },
            },
            "size": {"value": product.get("packSizeDescription")},
        }
        return parse_entity(entity, self.department_for(product.get("categoryPath")) or "food")


def _json_after(html: str, marker: str) -> Any:
    """Decode the JSON value that starts at the first `{` after `marker`, or None."""
    idx = html.find(marker)
    if idx < 0:
        return None
    start = html.find("{", idx + len(marker))
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(html, start)
    except ValueError:
        return None
    return value


def product_entities(html: str) -> list[dict[str, Any]]:
    entities = _json_after(html, '"productEntities"')
    if not isinstance(entities, dict):
        return []
    return [e for e in entities.values() if isinstance(e, dict)]


def page_product(html: str, chain_product_id: str) -> dict[str, Any] | None:
    state = _json_after(html, "window.__QUERY_INITIAL_STATE__")
    if not isinstance(state, dict):
        return None
    for query in state.get("queries") or []:
        product = ((query.get("state") or {}).get("data") or {}).get("product") if isinstance(query, dict) else None
        if isinstance(product, dict) and str(product.get("retailerProductId")) == chain_product_id:
            return product
    return None


def _label_unit(label: str | None) -> str | None:
    s = (label or "").lower()
    if "litre" in s or "liter" in s or "litro" in s:
        return "l"
    if "kilo" in s or "kg" in s:
        return "kg"
    if "unit" in s or "each" in s or "piece" in s or "unid" in s:
        return "unit"
    return None


def parse_entity(e: dict[str, Any], department: str | None) -> RawListing | None:
    """One product from the page state; None when it has no price or isn't food/drink/household."""
    price_info = e.get("price") or {}
    # Display-only products come with an empty amount.
    price = euros_to_cents((price_info.get("current") or {}).get("amount") or None)
    ext = e.get("retailerProductId")
    name = (e.get("name") or "").strip()
    if department is None or price is None or not ext or not name:
        return None
    size_text = ((e.get("size") or {}).get("value") or "").strip() or None
    quantity = parse_quantity(size_text) or parse_quantity(name)
    # The size field is sometimes one unit of a pack ("6 x 200 ml" with size "600ml"); an explicit
    # pack in the name wins, and Alcampo's unit price (worked off the wrong size) is dropped.
    pack = pack_quantity(name)
    size_is_wrong = bool(
        pack and quantity and pack.unit == quantity.unit and abs(pack.value - quantity.value) > 0.01 * pack.value
    )
    if size_is_wrong:
        quantity = pack
    unit = price_info.get("unit") or {}
    unit_price = None
    if quantity and not size_is_wrong and _label_unit(unit.get("label")) == quantity.unit:
        unit_price = euros_to_cents((unit.get("current") or {}).get("amount") or None)
    path = e.get("categoryPath") or []
    return RawListing(
        chain_id="alcampo",
        chain_product_id=str(ext),
        name=name,
        brand=(e.get("brand") or "").strip() or None,
        price_cents=price,
        unit_price_cents=unit_price,
        quantity=quantity,
        size_text=size_text,
        department=department,
        category=path[-1] if path else None,
        image_url=(e.get("image") or {}).get("src"),
        url=f"{BASE}/products/{ext}",
    )
