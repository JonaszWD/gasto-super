"""Mercadona's unofficial storefront API (tienda.mercadona.es/api).

Researched 2026-09 (see README "Price sources"):
- No auth. Prices depend on the warehouse, which Mercadona derives from the postal code
  (PUT /api/postal-codes/actions/change-pc/ returns it in the `x-customer-wh` header).
- GET /api/categories/?wh=..         -> category tree (top level + subcategories)
- GET /api/categories/<sub>/?wh=..   -> products with price_instructions (no EAN)
- GET /api/products/<id>/?wh=..      -> one product, including `ean`
- No public search endpoint (the site searches through a third-party index), so search()
  is not supported; searches read our database anyway.
Risks: undocumented, can change or block at any time; robots.txt disallows /api. We keep it to
one request every ~1.5 s, one warehouse, food/drink/household categories only, and fetch each
product's EAN only once.
"""

from collections.abc import AsyncIterator
from typing import Any

from app.services.catalog import RawListing
from app.services.quantity import Quantity
from app.sources.base import SourceAdapter, SourceContext, SourceError, euros_to_cents

BASE = "https://tienda.mercadona.es/api"

SIZE_UNITS = {"kg": ("kg", 1.0), "g": ("kg", 0.001), "l": ("l", 1.0), "ml": ("l", 0.001), "cl": ("l", 0.01),
              "ud": ("unit", 1.0), "uds": ("unit", 1.0), "unidad": ("unit", 1.0)}
REFERENCE_UNITS = {"kg": "kg", "l": "l", "ud": "unit", "unidad": "unit", "docena": None}


class MercadonaAdapter(SourceAdapter):
    id = "mercadona"
    chains = ("mercadona",)
    location_specific = True
    supports_live_refresh = True

    def __init__(self, departments: dict[str, list[int]] | None = None, subcategory_overrides: dict[str, list[int]] | None = None) -> None:
        # top-level category id -> department; subcategory overrides allow e.g. only baby *food*.
        self.top_departments: dict[int, str] = {}
        for dept, ids in (departments or {}).items():
            for cid in ids:
                self.top_departments[cid] = dept
        self.sub_departments: dict[int, str] = {}
        for dept, ids in (subcategory_overrides or {}).items():
            for cid in ids:
                self.sub_departments[cid] = dept

    async def warehouse(self, ctx: SourceContext) -> str:
        key = f"mercadona:wh:{ctx.postal_code}"
        cached = ctx.cache.get(key)
        if cached:
            return cached
        resp = await ctx.http.request(
            "PUT", f"{BASE}/postal-codes/actions/change-pc/", json={"new_postal_code": ctx.postal_code}
        )
        wh = resp.headers.get("x-customer-wh")
        if resp.status_code >= 400 or not wh:
            raise SourceError(f"Mercadona: no warehouse for postal code {ctx.postal_code} (HTTP {resp.status_code})")
        ctx.cache.set(key, wh)
        return wh

    def _department_for(self, top_id: int, sub_id: int) -> str | None:
        return self.sub_departments.get(sub_id) or self.top_departments.get(top_id)

    async def fetch_catalog(self, ctx: SourceContext) -> AsyncIterator[RawListing]:
        wh = await self.warehouse(ctx)
        params = {"lang": "es", "wh": wh}
        tree = await ctx.http.get_json(f"{BASE}/categories/", params=params)
        for top in tree.get("results", []):
            for sub in top.get("categories", []):
                dept = self._department_for(top["id"], sub["id"])
                if dept is None:
                    continue
                data = await ctx.http.get_json(f"{BASE}/categories/{sub['id']}/", params=params)
                for group in data.get("categories", []):
                    for product in group.get("products", []):
                        listing = parse_product(product, dept, sub.get("name"))
                        if listing:
                            yield listing

    async def fetch_product(self, ctx: SourceContext, chain_product_id: str) -> RawListing | None:
        wh = await self.warehouse(ctx)
        resp = await ctx.http.request("GET", f"{BASE}/products/{chain_product_id}/", params={"lang": "es", "wh": wh})
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise SourceError(f"Mercadona product {chain_product_id}: HTTP {resp.status_code}")
        data = resp.json()
        cats = data.get("categories") or []
        top_id = cats[0]["id"] if cats else -1
        sub = (cats[0].get("categories") or [{}])[0] if cats else {}
        dept = self._department_for(top_id, sub.get("id", -1)) or "food"
        return parse_product(data, dept, sub.get("name"))


def _quantity(pi: dict[str, Any]) -> Quantity | None:
    size, fmt = pi.get("unit_size"), (pi.get("size_format") or "").strip().lower()
    if not size or fmt not in SIZE_UNITS:
        return None
    base, factor = SIZE_UNITS[fmt]
    return Quantity(round(float(size) * factor, 6), base)


def parse_product(p: dict[str, Any], department: str, category: str | None) -> RawListing | None:
    pi = p.get("price_instructions") or {}
    price = euros_to_cents(pi.get("unit_price"))
    if price is None or not p.get("id") or not p.get("display_name"):
        return None
    quantity = _quantity(pi)
    unit_price = None
    ref_unit = REFERENCE_UNITS.get((pi.get("reference_format") or "").strip().lower())
    if ref_unit and quantity and ref_unit == quantity.unit:
        unit_price = euros_to_cents(pi.get("reference_price"))
    size = f"{float(pi['unit_size']):g}" if pi.get("unit_size") else ""
    size_text = " ".join(x for x in (p.get("packaging"), size, pi.get("size_format")) if x) or None
    return RawListing(
        chain_id="mercadona",
        chain_product_id=str(p["id"]),
        name=p["display_name"].strip(),
        brand=(p.get("brand") or None),
        ean=p.get("ean") or None,
        price_cents=price,
        unit_price_cents=unit_price,
        quantity=quantity,
        size_text=size_text,
        department=department,
        category=category,
        image_url=p.get("thumbnail"),
        url=p.get("share_url"),
    )
