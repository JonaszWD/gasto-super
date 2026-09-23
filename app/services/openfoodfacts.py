"""Open Food Facts v2 product lookup (server-side only)."""

import logging
from dataclasses import dataclass

import httpx

from app.config import Settings

log = logging.getLogger(__name__)

FIELDS = ",".join(
    [
        "code",
        "product_name",
        "product_name_es",
        "generic_name_es",
        "brands",
        "quantity",
        "categories_tags",
        "image_front_small_url",
        "image_front_url",
        "image_url",
    ]
)

# Keyword in an OFF category tag -> app category (see app/categories.py). First match wins,
# so more specific keywords come first.
CATEGORY_KEYWORDS: list[tuple[str, str]] = [
    ("baby", "Bebé"),
    ("pet-food", "Mascotas"),
    ("alcoholic-beverages", "Bebidas"),
    ("wines", "Bebidas"),
    ("beers", "Bebidas"),
    ("waters", "Bebidas"),
    ("beverages", "Bebidas"),
    ("dairies", "Lácteos y huevos"),
    ("cheeses", "Lácteos y huevos"),
    ("yogurts", "Lácteos y huevos"),
    ("milks", "Lácteos y huevos"),
    ("eggs", "Lácteos y huevos"),
    ("frozen", "Congelados"),
    ("meats", "Carne y pescado"),
    ("fishes", "Carne y pescado"),
    ("seafood", "Carne y pescado"),
    ("breads", "Panadería y dulces"),
    ("biscuits", "Panadería y dulces"),
    ("cakes", "Panadería y dulces"),
    ("chocolates", "Panadería y dulces"),
    ("sweet-snacks", "Panadería y dulces"),
    ("breakfast", "Panadería y dulces"),
    ("salty-snacks", "Aperitivos"),
    ("snacks", "Aperitivos"),
    ("fruits", "Fruta y verdura"),
    ("vegetables", "Fruta y verdura"),
    ("legumes", "Despensa"),
    ("cereals", "Despensa"),
    ("pastas", "Despensa"),
    ("rices", "Despensa"),
    ("oils", "Despensa"),
    ("sauces", "Despensa"),
    ("canned", "Despensa"),
    ("condiments", "Despensa"),
    ("plant-based-foods", "Despensa"),
]


@dataclass(frozen=True)
class OffProduct:
    barcode: str
    name: str
    brand: str | None
    category: str | None
    image_url: str | None


class OffLookupError(Exception):
    """Network or server problem talking to Open Food Facts."""


def map_category(tags: list[str] | None) -> str | None:
    if not tags:
        return None
    # Tags go from generic to specific; check specific ones first.
    for tag in reversed(tags):
        for keyword, category in CATEGORY_KEYWORDS:
            if keyword in tag:
                return category
    return None


def parse_product(barcode: str, payload: dict) -> OffProduct | None:
    if payload.get("status") != 1 or not isinstance(payload.get("product"), dict):
        return None
    p = payload["product"]
    name = (p.get("product_name_es") or p.get("product_name") or p.get("generic_name_es") or "").strip()
    if not name:
        return None
    quantity = (p.get("quantity") or "").strip()
    if quantity and quantity.lower() not in name.lower():
        name = f"{name} ({quantity})"
    brand = (p.get("brands") or "").split(",")[0].strip() or None
    image = p.get("image_front_small_url") or p.get("image_front_url") or p.get("image_url")
    return OffProduct(
        barcode=barcode,
        name=name,
        brand=brand,
        category=map_category(p.get("categories_tags")),
        image_url=image,
    )


class OpenFoodFactsClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport

    async def lookup(self, barcode: str) -> OffProduct | None:
        """Return the product, None if OFF does not know it, or raise OffLookupError."""
        url = f"{self._settings.off_base_url}/api/v2/product/{barcode}.json"
        headers = {"User-Agent": self._settings.off_user_agent, "Accept": "application/json"}
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.off_timeout_seconds, transport=self._transport
            ) as client:
                resp = await client.get(url, params={"fields": FIELDS, "lc": "es"}, headers=headers)
        except httpx.HTTPError as exc:
            log.warning("Open Food Facts request failed for %s: %s", barcode, exc)
            raise OffLookupError(str(exc)) from exc

        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise OffLookupError(f"HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise OffLookupError("invalid JSON") from exc
        return parse_product(barcode, payload)
