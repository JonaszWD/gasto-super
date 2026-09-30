"""Writing collected listings: upsert, EAN linking to canonical products, price recording."""

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import bindparam, tuple_
from sqlmodel import Session, col, select, update

from app.models import CanonicalProduct, Listing, ProductMatch, ShoppingListItem, utcnow
from app.services.prices import Observation, apply_observation, latest_prices, record_observation
from app.services.product_types import classify
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
    if listing is None:
        listing = _new_listing(source, raw, postal_code)
    _apply_raw(listing, raw)
    session.add(listing)
    session.flush()
    link_listing(session, listing)
    return listing, is_new


def _new_listing(source: str, raw: RawListing, postal_code: str) -> Listing:
    now = raw.observed_at or utcnow()
    return Listing(
        source=source,
        chain_id=raw.chain_id,
        chain_product_id=raw.chain_product_id,
        postal_code=postal_code,
        name=raw.name,
        department=raw.department,
        first_seen_at=now,
        last_seen_at=now,
    )


def _apply_raw(listing: Listing, raw: RawListing) -> None:
    now = raw.observed_at or utcnow()
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


def store_listings(session: Session, source: str, raws: list[RawListing], postal_code: str) -> list[tuple[bool, bool]]:
    """upsert_listing + record_listing_price for many products in a handful of round trips.

    Collectors run far from the database (GitHub's US runners, Neon in Frankfurt), so the
    per-product version spends ~1 s per product waiting on the network. Here existing rows are
    loaded with one query each, the same rules are applied in memory, and the writes go out as
    batched INSERTs/UPDATEs. The rare EAN re-linking cases (an existing listing gaining or
    changing its EAN) still go through link_listing. Returns (is_new, price_changed) per raw.
    Any error propagates: the caller retries the batch product by product.
    """
    if not raws:
        return []
    keys = {(r.chain_id, r.chain_product_id) for r in raws}
    by_key: dict[tuple[str, str], Listing] = {
        (li.chain_id, li.chain_product_id): li
        for li in session.exec(
            select(Listing).where(
                Listing.source == source,
                Listing.postal_code == postal_code,
                tuple_(col(Listing.chain_id), col(Listing.chain_product_id)).in_(list(keys)),
            )
        ).all()
    }
    eans = {e for r in raws if (e := valid_ean(r.ean))} | {e for li in by_key.values() if (e := valid_ean(li.ean))}
    canon_by_ean: dict[str, CanonicalProduct] = (
        {c.ean: c for c in session.exec(select(CanonicalProduct).where(col(CanonicalProduct.ean).in_(eans))).all() if c.ean}
        if eans
        else {}
    )

    new_listings: list[Listing] = []
    pending_link: dict[int, tuple[Listing, CanonicalProduct, str]] = {}  # id(listing) -> link to make
    relink: dict[int, Listing] = {}
    results: list[tuple[Listing, bool]] = []
    for raw in raws:
        key = (raw.chain_id, raw.chain_product_id)
        listing = by_key.get(key)
        is_new = listing is None
        if listing is None:
            listing = _new_listing(source, raw, postal_code)
            by_key[key] = listing
            new_listings.append(listing)
        _apply_raw(listing, raw)
        results.append((listing, is_new))
        if listing.link_source == "manual" or id(listing) in pending_link:
            continue
        ean = valid_ean(listing.ean)
        canon = canon_by_ean.get(ean) if ean else None
        if listing.canonical_product_id is None:
            if canon is None:
                canon = _canonical_from_listing(listing, ean)
                session.add(canon)
                if ean:
                    canon_by_ean[ean] = canon
            pending_link[id(listing)] = (listing, canon, "ean" if ean else "own")
        elif ean and (canon is None or canon.id != listing.canonical_product_id):
            relink[id(listing)] = listing  # gained or changed its EAN: needs the full linking rules

    session.flush()  # new canonical products (batched), updated listings
    for listing, canon, link_source in pending_link.values():
        listing.canonical_product_id = canon.id
        listing.link_source = link_source
    session.add_all(new_listings)
    session.flush()  # new listings (batched), so they have ids
    for listing in relink.values():
        link_listing(session, listing)

    latest = latest_prices(session, [li.id for li, is_new in results if not is_new and li.id is not None])
    out: list[tuple[bool, bool]] = []
    for (listing, is_new), raw in zip(results, raws):
        assert listing.id is not None
        obs = Observation(raw.price_cents, raw.resolved_unit_price(), raw.observed_at or utcnow())
        current = latest.get(listing.id)
        new_row = apply_observation(current, listing.id, obs)
        if new_row is not None:
            session.add(new_row)
            latest[listing.id] = new_row
        out.append((is_new, new_row is not None))
    session.flush()
    return out


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
        product_type=classify(listing.name, listing.brand, listing.category, listing.department),
    )


def retype(session: Session) -> int:
    """Re-apply app/product_types.toml to every canonical product; returns how many changed.

    One read and one batched UPDATE, so it is cheap enough to run after every collection.
    """
    rows = session.exec(
        select(
            CanonicalProduct.id, CanonicalProduct.name, CanonicalProduct.brand, CanonicalProduct.category,
            CanonicalProduct.department, CanonicalProduct.product_type,
        )
    ).all()
    changes = [
        {"pid": pid, "ptype": new}
        for pid, name, brand, category, department, current in rows
        if (new := classify(name, brand, category, department)) != current
    ]
    if changes:
        stmt = (
            update(CanonicalProduct)
            .where(col(CanonicalProduct.id) == bindparam("pid"))
            .values(product_type=bindparam("ptype"))
            .execution_options(synchronize_session=False)
        )
        session.connection().execute(stmt, changes)
    return len(changes)


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
