"""Price history stores a row only when the price changes."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from app.models import ListingPrice
from app.services.catalog import RawListing, record_listing_price, upsert_listing
from app.services.prices import Observation, is_stale, record_observation

T0 = datetime(2026, 9, 1, 2, 0, tzinfo=UTC)


def _listing(session: Session) -> int:
    raw = RawListing(chain_id="mercadona", chain_product_id="1", name="Leche entera 1 L", price_cents=95, department="food")
    listing, _ = upsert_listing(session, "mercadona", raw, "28020")
    assert listing.id is not None
    return listing.id


def _rows(session: Session, listing_id: int) -> list[ListingPrice]:
    return list(
        session.exec(select(ListingPrice).where(ListingPrice.listing_id == listing_id).order_by(col(ListingPrice.id))).all()
    )


def test_unchanged_price_only_moves_last_seen(clean_db: Engine) -> None:
    with Session(clean_db) as s:
        lid = _listing(s)
        assert record_observation(s, lid, Observation(95, 95, T0)) is True
        assert record_observation(s, lid, Observation(95, 95, T0 + timedelta(days=1))) is False
        assert record_observation(s, lid, Observation(95, 95, T0 + timedelta(days=2))) is False
        rows = _rows(s, lid)
        assert len(rows) == 1
        assert rows[0].first_seen_at == T0
        assert rows[0].last_seen_at == T0 + timedelta(days=2)


def test_changed_price_adds_row_and_keeps_history(clean_db: Engine) -> None:
    with Session(clean_db) as s:
        lid = _listing(s)
        record_observation(s, lid, Observation(95, 95, T0))
        assert record_observation(s, lid, Observation(99, 99, T0 + timedelta(days=1))) is True
        record_observation(s, lid, Observation(99, 99, T0 + timedelta(days=2)))
        # Back to the old price: a new period, the old row is not reused.
        assert record_observation(s, lid, Observation(95, 95, T0 + timedelta(days=3))) is True
        rows = _rows(s, lid)
        assert [(r.price_cents, r.first_seen_at.day, r.last_seen_at.day) for r in rows] == [(95, 1, 1), (99, 2, 3), (95, 4, 4)]


def test_unit_price_change_counts_as_change(clean_db: Engine) -> None:
    with Session(clean_db) as s:
        lid = _listing(s)
        record_observation(s, lid, Observation(95, 95, T0))
        assert record_observation(s, lid, Observation(95, 190, T0 + timedelta(days=1))) is True  # pack size changed


def test_older_observation_is_ignored(clean_db: Engine) -> None:
    with Session(clean_db) as s:
        lid = _listing(s)
        record_observation(s, lid, Observation(95, 95, T0))
        assert record_observation(s, lid, Observation(80, 80, T0 - timedelta(days=5))) is False
        assert len(_rows(s, lid)) == 1


def test_record_listing_price_via_raw(clean_db: Engine) -> None:
    with Session(clean_db) as s:
        raw = RawListing(
            chain_id="mercadona", chain_product_id="1", name="Aceite", price_cents=1725, department="food", size_text="5 L"
        )
        listing, is_new = upsert_listing(s, "mercadona", raw, "28020")
        assert is_new
        assert record_listing_price(s, listing, raw) is True
        assert record_listing_price(s, listing, raw) is False
        again, is_new = upsert_listing(s, "mercadona", raw, "28020")
        assert not is_new and again.id == listing.id
        assert _rows(s, listing.id)[0].unit_price_cents == 345  # type: ignore[arg-type]


def test_is_stale_uses_last_seen() -> None:
    now = T0 + timedelta(days=10)
    assert is_stale(T0, now, 7)
    assert not is_stale(T0 + timedelta(days=4), now, 7)
