"""Price history that only grows when a price changes (keeps Neon's 0.5 GB free tier happy).

Each ListingPrice row is a period during which the price was constant:
first_seen_at = first observation of that price, last_seen_at = latest observation.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlmodel import Session, col, select

from app.models import ListingPrice


@dataclass(frozen=True)
class Observation:
    price_cents: int
    unit_price_cents: int | None
    seen_at: datetime


def latest_price(session: Session, listing_id: int) -> ListingPrice | None:
    stmt = (
        select(ListingPrice)
        .where(ListingPrice.listing_id == listing_id)
        .order_by(col(ListingPrice.last_seen_at).desc(), col(ListingPrice.id).desc())
        .limit(1)
    )
    return session.exec(stmt).first()


def record_observation(session: Session, listing_id: int, obs: Observation) -> bool:
    """Store an observed price. Returns True when a new price row was inserted.

    - Same price as the latest row: only last_seen_at moves forward (no new row).
    - Different price, observed after the latest row: new row.
    - Observation older than what we already know: ignored (history is append-only in time).
    """
    current = latest_price(session, listing_id)
    if current is not None:
        if obs.seen_at < current.last_seen_at:
            return False
        if current.price_cents == obs.price_cents and current.unit_price_cents == obs.unit_price_cents:
            current.last_seen_at = obs.seen_at
            session.add(current)
            return False
    session.add(
        ListingPrice(
            listing_id=listing_id,
            price_cents=obs.price_cents,
            unit_price_cents=obs.unit_price_cents,
            first_seen_at=obs.seen_at,
            last_seen_at=obs.seen_at,
        )
    )
    return True


def is_stale(last_seen_at: datetime, now: datetime, max_age_days: int = 7) -> bool:
    """A price not seen for more than `max_age_days` is stale."""
    return now - last_seen_at > timedelta(days=max_age_days)
