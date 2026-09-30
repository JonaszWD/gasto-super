from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer

from app.services.barcode_parser import BarcodeKind
from app.services.money import parse_decimal, to_cents


def _as_utc(dt: datetime) -> str:
    # Always emit UTC with "Z" so the browser converts to Europe/Madrid.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


UtcDatetime = Annotated[datetime, PlainSerializer(_as_utc, return_type=str)]
# Accepts "1,25", "1.25", 1.25 or "1.234,56" and yields integer cents.
PriceInput = Annotated[int, BeforeValidator(to_cents)]
QuantityInput = Annotated[
    float,
    BeforeValidator(lambda v: float(parse_decimal(v, max_decimals=3))),
    Field(gt=0, le=10000),
]
Name = Annotated[str, Field(min_length=1, max_length=200)]


class StoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str


class StoreCreate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=80)]


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    key: str
    name: str
    brand: str | None = None
    category: str | None = None
    image_url: str | None = None
    source: str


class ProductUpsert(BaseModel):
    name: Name
    category: str | None = Field(default=None, max_length=80)
    brand: str | None = Field(default=None, max_length=80)


class ScanResult(BaseModel):
    barcode: str
    product_key: str
    kind: BarcodeKind
    valid_checksum: bool
    found: bool
    source: str  # "cache" | "off" | "none"
    lookup_error: bool = False
    product: ProductOut | None = None
    embedded_price_cents: int | None = None
    weight_grams: int | None = None
    last_unit_price_cents: int | None = None
    suggested_unit_price_cents: int | None = None
    suggested_quantity: float = 1.0


class TripCreate(BaseModel):
    store_id: int


class PurchaseCreate(BaseModel):
    barcode: Annotated[str, Field(min_length=1, max_length=64)]
    name: Name | None = None
    category: str | None = Field(default=None, max_length=80)
    unit_price: PriceInput
    quantity: QuantityInput = 1.0
    weight_grams: int | None = Field(default=None, ge=0)


class PurchaseUpdate(BaseModel):
    name: Name | None = None
    category: str | None = Field(default=None, max_length=80)
    unit_price: PriceInput | None = None
    quantity: QuantityInput | None = None


class PurchaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    trip_id: int
    barcode: str
    product_key: str
    name: str
    category: str | None
    unit_price_cents: int
    quantity: float
    total_cents: int
    weight_grams: int | None
    created_at: UtcDatetime


class TripOut(BaseModel):
    id: int
    store_id: int
    store_name: str
    started_at: UtcDatetime
    closed_at: UtcDatetime | None
    total_cents: int
    item_count: int


class TripDetail(TripOut):
    purchases: list[PurchaseOut]


class Bucket(BaseModel):
    label: str  # "2026-W38" / "2026-09" / category / store name
    start: str | None = None  # ISO date of the bucket start (time series only)
    total_cents: int


class StatsOut(BaseModel):
    weekly: list[Bucket]
    monthly: list[Bucket]
    by_category: list[Bucket]
    by_store: list[Bucket]
    range_total_cents: int


class PricePoint(BaseModel):
    date: UtcDatetime
    unit_price_cents: int
    store_name: str


class PriceHistory(BaseModel):
    product_key: str
    name: str
    points: list[PricePoint]


class ProductSummary(BaseModel):
    product_key: str
    name: str
    purchases: int
    last_unit_price_cents: int


# ---------------------------------------------------------------- price comparison


class ComparedProduct(BaseModel):
    id: int
    name: str
    brand: str | None
    ean: str | None
    department: str
    category: str | None
    quantity_value: float | None
    quantity_unit: str | None


class ChainPriceOut(BaseModel):
    chain_id: str
    chain_name: str
    match: str  # exact | confirmed | similar | none
    listing_id: int | None = None
    listing_name: str | None = None
    source: str | None = None
    price_cents: int | None = None
    unit_price_cents: int | None = None
    unit: str | None = None
    quantity_value: float | None = None
    last_seen_at: UtcDatetime | None = None
    stale: bool = False
    cheapest: bool = False
    location_specific: bool = False
    can_refresh: bool = False


class PaidPriceOut(BaseModel):
    store_name: str
    chain_id: str | None
    unit_price_cents: int
    paid_at: UtcDatetime


class ProductCardOut(BaseModel):
    product: ComparedProduct
    prices: list[ChainPriceOut]
    paid: list[PaidPriceOut]
    image_url: str | None = None


class SearchOut(BaseModel):
    query: str
    postal_code: str
    results: list[ProductCardOut]


class HistoryPoint(BaseModel):
    price_cents: int
    unit_price_cents: int | None
    first_seen_at: UtcDatetime
    last_seen_at: UtcDatetime


class ChainHistory(BaseModel):
    chain_id: str
    chain_name: str
    listing_id: int
    listing_name: str
    match: str
    source: str
    points: list[HistoryPoint]


class ProductDetailOut(ProductCardOut):
    history: list[ChainHistory]


class ProductTypeSummaryOut(BaseModel):
    slug: str
    name_es: str
    name_en: str
    products: int
    chains: int
    unit: str | None
    min_unit_price_cents: int | None
    min_chain_name: str | None


class ProductTypesOut(BaseModel):
    query: str
    types: list[ProductTypeSummaryOut]


class TypeOfferOut(BaseModel):
    product_id: int
    listing_id: int
    name: str
    price_cents: int
    unit_price_cents: int | None
    unit: str | None
    quantity_value: float | None
    last_seen_at: UtcDatetime
    stale: bool
    location_specific: bool


class TypeChainOut(BaseModel):
    chain_id: str
    chain_name: str
    cheapest: bool
    offers: list[TypeOfferOut]


class ProductTypeDetailOut(BaseModel):
    slug: str
    name_es: str
    name_en: str
    unit: str | None
    default_amount: float
    postal_code: str
    chains: list[TypeChainOut]


class MatchAction(BaseModel):
    listing_id: int
    action: str = Field(pattern="^(confirm|reject|relink|reset)$")


class Alternative(BaseModel):
    product_id: int
    chain_id: str
    chain_name: str
    price_cents: int
    unit_price_cents: int | None
    unit: str | None
    match: str
    reference_price_cents: int | None
    reference_label: str  # "entered" (price typed on the scan sheet) or a chain name
    savings_cents: int | None
    last_seen_at: UtcDatetime | None


class AlternativeOut(BaseModel):
    alternative: Alternative | None


Language = Literal["es", "en"]


class SettingsOut(BaseModel):
    postal_code: str
    language: Language


class SettingsIn(BaseModel):
    postal_code: str | None = Field(default=None, pattern=r"^\d{5}$")
    language: Language | None = None


class ListItemIn(BaseModel):
    product_id: int
    quantity: int = Field(default=1, ge=1, le=99)


class ListTypeIn(BaseModel):
    product_type: str = Field(max_length=60)
    amount: float = Field(gt=0, le=100)
    unit: Literal["kg", "l", "unit"]


class ListTypeAmountIn(BaseModel):
    amount: float = Field(gt=0, le=100)


class ListLineOut(BaseModel):
    key: str
    product_id: int | None
    name: str
    quantity: int
    price_cents: int | None
    match: str
    product_type: str | None = None
    name_en: str | None = None  # types only: products keep the chain's (Spanish) name
    amount: float | None = None
    unit: str | None = None
    chosen_product_id: int | None = None
    chosen_name: str | None = None
    packs: int | None = None


class ChainTotalOut(BaseModel):
    chain_id: str
    chain_name: str
    total_cents: int
    missing: int
    similar: int
    stale: int
    lines: list[ListLineOut]


class SplitOut(BaseModel):
    chain_ids: list[str]
    chain_names: list[str]
    total_cents: int
    missing: int
    assignment: dict[str, str]  # line key -> chain id


class ShoppingListOut(BaseModel):
    items: list[ListLineOut]
    chains: list[ChainTotalOut]
    split: SplitOut | None
