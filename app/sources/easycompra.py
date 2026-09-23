"""EasyCompra community dataset (github.com/elopositor/EasyCompra-datos).

Daily JSON files per chain, built by the EasyCompra project from the chains' public websites.
Used for chains whose own APIs sit behind bot protection (Dia, Carrefour) and for Lidl.
- Prices are NOT tied to a postal code (stored with postal_code "").
- Licence: personal, non-commercial use. Partial catalogues (hundreds of items per chain).
- index.json marks each chain `fresh`; stale chains are skipped so their prices age visibly.
One HTTP request per chain.
"""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from app.services.catalog import RawListing
from app.services.quantity import parse_quantity
from app.services.text import normalize
from app.sources.base import SourceAdapter, SourceContext, euros_to_cents

BASE = "https://raw.githubusercontent.com/elopositor/EasyCompra-datos/main"

REFERENCE_UNITS = {"kg": "kg", "kilo": "kg", "kilogramo": "kg", "l": "l", "litro": "l", "ud": "unit", "unidad": "unit"}

DRINK_WORDS = {
    "agua", "refresco", "cola", "cerveza", "vino", "cava", "sidra", "zumo", "nectar", "bebida",
    "tonica", "gaseosa", "cafe", "te", "infusion", "licor", "ginebra", "ron", "whisky", "vodka",
    "isotonica", "energetica", "horchata", "sangria", "vermut",
}
HOUSEHOLD_WORDS = {"detergente", "suavizante", "lejia", "friegasuelos", "lavavajillas", "papel", "servilletas",
                   "bayeta", "estropajo", "limpiador", "ambientador", "bolsas", "insecticida"}


def guess_department(name: str) -> str:
    words = set(normalize(name).split())
    if words & HOUSEHOLD_WORDS:
        return "household"
    if words & DRINK_WORDS and "leche" not in words:
        return "drink"
    return "food"


class EasyCompraAdapter(SourceAdapter):
    id = "easycompra"
    chains = ("dia", "carrefour", "lidl")
    location_specific = False
    supports_live_refresh = False
    min_interval = 1.0

    async def _index(self, ctx: SourceContext) -> dict[str, Any]:
        return await ctx.http.get_json(f"{BASE}/index.json")

    async def fetch_catalog(self, ctx: SourceContext) -> AsyncIterator[RawListing]:
        index = await self._index(ctx)
        updated = _parse_dt(index.get("updated_at"))
        for chain in self.chains:
            if not self.wants_chain(ctx, chain):
                continue
            meta = (index.get("supermarkets") or {}).get(chain)
            if not meta:
                continue
            if not meta.get("fresh", False):
                ctx.warnings.append(f"EasyCompra: {chain} data is marked not fresh; skipped")
                continue
            items = await ctx.http.get_json(f"{BASE}/{meta.get('file', chain + '.json')}")
            for item in items:
                listing = parse_item(chain, item, updated)
                if listing:
                    yield listing

    async def search(self, ctx: SourceContext, query: str) -> list[RawListing]:
        words = normalize(query).split()
        out = []
        async for listing in self.fetch_catalog(ctx):
            if all(w in normalize(listing.name) for w in words):
                out.append(listing)
        return out


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_item(chain: str, item: dict[str, Any], observed_at: datetime | None) -> RawListing | None:
    price = euros_to_cents(item.get("unit_price"))
    name = (item.get("name") or "").strip()
    ext = item.get("external_id") or item.get("id")
    if price is None or not name or not ext:
        return None
    quantity = parse_quantity(name)
    unit_price = None
    ref_unit = REFERENCE_UNITS.get(normalize(item.get("reference_format") or ""))
    if ref_unit and quantity and ref_unit == quantity.unit and item.get("reference_price") is not None:
        unit_price = euros_to_cents(item.get("reference_price"))
    return RawListing(
        chain_id=chain,
        chain_product_id=str(ext),
        name=name,
        brand=item.get("brand") or None,
        ean=item.get("ean") or None,
        price_cents=price,
        unit_price_cents=unit_price,
        quantity=quantity,
        department=guess_department(name),
        image_url=item.get("photo_url"),
        url=item.get("share_url"),
        observed_at=observed_at,
    )
