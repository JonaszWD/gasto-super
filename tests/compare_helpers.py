from datetime import datetime

from sqlmodel import Session

from app.models import Listing
from app.services.catalog import RawListing, record_listing_price, upsert_listing


def add_listing(
    session: Session,
    chain: str,
    pid: str,
    name: str,
    price: int,
    *,
    ean: str | None = None,
    size: str | None = None,
    department: str = "food",
    source: str | None = None,
    postal_code: str | None = None,
    brand: str | None = None,
    category: str | None = None,
    seen: datetime | None = None,
    price_label: str | None = None,
) -> Listing:
    source = source or ("mercadona" if chain == "mercadona" else "easycompra")
    pc = postal_code if postal_code is not None else ("28020" if source == "mercadona" else "")
    raw = RawListing(
        chain_id=chain, chain_product_id=pid, name=name, price_cents=price, ean=ean, size_text=size,
        department=department, brand=brand, category=category, observed_at=seen,
        price_label=price_label,
    )
    listing, _ = upsert_listing(session, source, raw, pc)
    record_listing_price(session, listing, raw)
    session.commit()
    session.refresh(listing)
    return listing
