"""Database tables. Plain Postgres types only; every timestamp is timestamptz in UTC."""

from datetime import UTC, datetime

from sqlalchemy import Index, Numeric, UniqueConstraint, text
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- spending tracker


class Store(SQLModel, table=True):
    __table_args__ = (Index("uq_store_name_lower", text("lower(name)"), unique=True),)

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(max_length=80)
    # Key into store_rules.json for variable-weight barcode layouts.
    rules_key: str | None = Field(default=None, max_length=80)
    # Chain this store belongs to, so paid prices show up in the price comparison.
    chain_id: str | None = Field(default=None, foreign_key="chain.id", max_length=40)


class Product(SQLModel, table=True):
    # EAN/UPC for regular items, or "vw:<prefix>:<item>" for variable-weight labels.
    key: str = Field(primary_key=True, max_length=80)
    name: str = Field(max_length=200)
    brand: str | None = Field(default=None, max_length=120)
    category: str | None = Field(default=None, max_length=80)
    image_url: str | None = Field(default=None, max_length=500)
    # "off" = Open Food Facts, "manual" = typed by the user.
    source: str = Field(default="manual", max_length=20)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Trip(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    store_id: int = Field(foreign_key="store.id", index=True)
    started_at: datetime = Field(default_factory=utcnow, index=True)
    closed_at: datetime | None = None


class Purchase(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    trip_id: int = Field(foreign_key="trip.id", index=True, ondelete="CASCADE")
    barcode: str = Field(max_length=64)
    product_key: str = Field(index=True, max_length=80)
    # Denormalized copy of the product name/category; kept in sync when the product is renamed.
    name: str = Field(max_length=200)
    category: str | None = Field(default=None, max_length=80)
    unit_price_cents: int
    quantity: float = Field(default=1.0, sa_type=Numeric(10, 3, asdecimal=False))
    total_cents: int
    weight_grams: int | None = None
    created_at: datetime = Field(default_factory=utcnow, index=True)


# ---------------------------------------------------------------- price comparison


class Chain(SQLModel, table=True):
    id: str = Field(primary_key=True, max_length=40)  # "mercadona"
    name: str = Field(max_length=80)


class CanonicalProduct(SQLModel, table=True):
    """The product itself, independent of who sells it."""

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(max_length=200)
    brand: str | None = Field(default=None, max_length=120)
    ean: str | None = Field(default=None, unique=True, max_length=20)
    department: str = Field(max_length=20)  # food | drink | household
    category: str | None = Field(default=None, max_length=120)
    # Total amount in the base unit (kg, l or unit) and that unit.
    quantity_value: float | None = Field(default=None, sa_type=Numeric(12, 4, asdecimal=False))
    quantity_unit: str | None = Field(default=None, max_length=8)
    # Lower-case, accent-free name + brand for portable LIKE search.
    search_text: str = Field(default="", max_length=400)
    created_at: datetime = Field(default_factory=utcnow)


class Listing(SQLModel, table=True):
    """A product as offered by one chain, for one postal code ('' when not location-specific)."""

    __table_args__ = (UniqueConstraint("source", "chain_id", "chain_product_id", "postal_code"),)

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(max_length=20)  # adapter that produced it: mercadona | easycompra | openprices
    chain_id: str = Field(foreign_key="chain.id", index=True, max_length=40)
    chain_product_id: str = Field(max_length=80)
    postal_code: str = Field(default="", max_length=10)
    canonical_product_id: int | None = Field(default=None, foreign_key="canonicalproduct.id", index=True)
    # How canonical_product_id was set: ean | own | manual. Collectors never touch "manual" links.
    link_source: str = Field(default="own", max_length=10)
    ean: str | None = Field(default=None, index=True, max_length=20)
    name: str = Field(max_length=200)
    brand: str | None = Field(default=None, max_length=120)
    department: str = Field(max_length=20)
    category: str | None = Field(default=None, max_length=120)
    size_text: str | None = Field(default=None, max_length=80)
    quantity_value: float | None = Field(default=None, sa_type=Numeric(12, 4, asdecimal=False))
    quantity_unit: str | None = Field(default=None, max_length=8)
    image_url: str | None = Field(default=None, max_length=500)
    url: str | None = Field(default=None, max_length=500)
    search_text: str = Field(default="", max_length=400)
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow, index=True)


class ListingPrice(SQLModel, table=True):
    """A price period: a new row only when the price changes; otherwise last_seen_at moves on."""

    __table_args__ = (Index("ix_listingprice_listing_last_seen", "listing_id", "last_seen_at"),)

    id: int | None = Field(default=None, primary_key=True)
    listing_id: int = Field(foreign_key="listing.id", ondelete="CASCADE")
    price_cents: int
    # Price per kg / l / unit (per Listing.quantity_unit), in cents.
    unit_price_cents: int | None = None
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)


class ProductMatch(SQLModel, table=True):
    """Manual decision about a *similar* match. Takes priority over automatic matching."""

    __table_args__ = (UniqueConstraint("canonical_product_id", "listing_id"),)

    id: int | None = Field(default=None, primary_key=True)
    canonical_product_id: int = Field(foreign_key="canonicalproduct.id", index=True, ondelete="CASCADE")
    listing_id: int = Field(foreign_key="listing.id", ondelete="CASCADE")
    status: str = Field(max_length=10)  # confirmed | rejected
    created_at: datetime = Field(default_factory=utcnow)


class ShoppingListItem(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    canonical_product_id: int = Field(foreign_key="canonicalproduct.id", unique=True, ondelete="CASCADE")
    quantity: int = 1
    created_at: datetime = Field(default_factory=utcnow)


class CollectorRun(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(max_length=20)
    postal_code: str = Field(default="", max_length=10)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    status: str = Field(default="running", max_length=10)  # running | ok | partial | failed
    products_checked: int = 0
    prices_changed: int = 0
    new_listings: int = 0
    errors: int = 0
    error_messages: str = ""


# ---------------------------------------------------------------- app


class AppSetting(SQLModel, table=True):
    key: str = Field(primary_key=True, max_length=40)
    value: str = Field(max_length=200)


class LoginAttempt(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    ip: str = Field(max_length=64, index=True)
    success: bool
    attempted_at: datetime = Field(default_factory=utcnow, index=True)
