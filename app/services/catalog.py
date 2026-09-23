"""Writing collected listings: upsert, EAN linking to canonical products, price recording."""

from dataclasses import dataclass, field
from datetime import datetime

from sqlmodel import Session, col, select, update

from app.models import CanonicalProduct, Listing, ProductMatch, ShoppingListItem, utcnow
from app.services.prices import Observation, record_observation
from app.services.quantity import Quantity, parse_quantity, unit_price_cents
from app.services.text import search_text

DEPARTMENTS = ("food", "drink", "household")


@dataclass
class RawListing:
    """What a source adapter returns for one product at one chain."""

    chain_id: str
    chain_product_id: str
    name: str
    price_cents: int
    department: str
    brand: str | None = None
    ean: str | None = None
    category: str | None = None
    size_text: str | None = None
    quantity: Quantity | None = None
    unit_price_cents: int | None = None
    image_url: str | None = None
    url: str | None = None
    # When the price was observed; None = now.
    observed_at: datetime | None = None
    extra: dict = field(default_factory=dict)

    def resolved_quantity(self) -> Quantity | None:
        return self.quantity or parse_quantity(self.size_text) or parse_quantity(self.name)

    def resolved_unit_price(self) -> int | None:
        if self.unit_price_cents is not None:
            return self.unit_price_cents
        return unit_price_cents(self.price_cents, self.resolved_quantity())


def valid_ean(ean: str | None) -> str | None:
    if ean and ean.isdigit() and len(ean) in (8, 12, 13, 14):
        return ean.zfill(13) if len(ean) == 12 else ean
    return None


def upsert_listing(session: Session, source: str, raw: RawListing, postal_code: str) -> tuple[Listing, bool]:
    listing = session.exec(
        select(Listing).where(
            Listing.source == source,
            Listing.chain_id == raw.chain_id,
            Listing.chain_product_id == raw.chain_product_id,
            Listing.postal_code == postal_code,
        )
    ).first()
    is_new = listing is None
    now = raw.observed_at or utcnow()
    if listing is None:
        listing = Listing(
            source=source,
            chain_id=raw.chain_id,
            chain_product_id=raw.chain_product_id,
            postal_code=postal_code,
            name=raw.name,
            department=raw.department,
            first_seen_at=now,
            last_seen_at=now,
        )
    q = raw.resolved_quantity()
    listing.name = raw.name[:200]
    listing.brand = raw.brand
    listing.department = raw.department
    listing.category = raw.category
    listing.size_text = raw.size_text
    listing.quantity_value = q.value if q else None
    listing.quantity_unit = q.unit if q else None
    listing.image_url = raw.image_url
    listing.url = raw.url
    listing.ean = valid_ean(raw.ean) or listing.ean
    listing.search_text = search_text(raw.name, raw.brand)
    if now > listing.last_seen_at:
        listing.last_seen_at = now
    session.add(listing)
    session.flush()
    link_listing(session, listing)
    return listing, is_new


def record_listing_price(session: Session, listing: Listing, raw: RawListing) -> bool:
    assert listing.id is not None
    return record_observation(
        session,
        listing.id,
        Observation(raw.price_cents, raw.resolved_unit_price(), raw.observed_at or utcnow()),
    )


def _canonical_from_listing(listing: Listing, ean: str | None) -> CanonicalProduct:
    return CanonicalProduct(
        name=listing.name,
        brand=listing.brand,
        ean=ean,
        department=listing.department,
        category=listing.category,
        quantity_value=listing.quantity_value,
        quantity_unit=listing.quantity_unit,
        search_text=listing.search_text,
    )


def merge_canonical(session: Session, old_id: int, new_id: int) -> None:
    """Move everything that points at canonical `old_id` to `new_id`, then delete `old_id`."""
    if old_id == new_id:
        return
    session.exec(update(Listing).where(col(Listing.canonical_product_id) == old_id).values(canonical_product_id=new_id))
    for item in session.exec(select(ShoppingListItem).where(ShoppingListItem.canonical_product_id == old_id)).all():
        existing = session.exec(select(ShoppingListItem).where(ShoppingListItem.canonical_product_id == new_id)).first()
        if existing:
            existing.quantity += item.quantity
            session.delete(item)
        else:
            item.canonical_product_id = new_id
    for match in session.exec(select(ProductMatch).where(ProductMatch.canonical_product_id == old_id)).all():
        session.delete(match)
    session.flush()
    old = session.get(CanonicalProduct, old_id)
    if old:
        session.delete(old)
    session.flush()


def link_listing(session: Session, listing: Listing) -> None:
    """Exact matching: listings sharing an EAN share a canonical product. Manual links win."""
    if listing.link_source == "manual":
        return
    ean = valid_ean(listing.ean)
    if ean:
        canonical = session.exec(select(CanonicalProduct).where(CanonicalProduct.ean == ean)).first()
        if canonical is None:
            own = session.get(CanonicalProduct, listing.canonical_product_id) if listing.canonical_product_id else None
            if own is not None and own.ean is None:
                own.ean = ean  # upgrade the listing's own product in place
                session.add(own)
                canonical = own
            else:
                canonical = _canonical_from_listing(listing, ean)
                session.add(canonical)
                session.flush()
        assert canonical.id is not None
        old_id = listing.canonical_product_id
        listing.canonical_product_id = canonical.id
        listing.link_source = "ean"
        session.add(listing)
        session.flush()
        if old_id and old_id != canonical.id:
            still_used = session.exec(select(Listing.id).where(Listing.canonical_product_id == old_id)).first()
            if still_used is None:
                merge_canonical(session, old_id, canonical.id)
        return
    if listing.canonical_product_id is None:
        canonical = _canonical_from_listing(listing, None)
        session.add(canonical)
        session.flush()
        listing.canonical_product_id = canonical.id
        listing.link_source = "own"
        session.add(listing)
        session.flush()
