"""Batched collector writes (store_listings) follow the same rules as upsert_listing + record_listing_price."""

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import Engine, event
from sqlmodel import Session, col, select

from app.models import CanonicalProduct, Listing, ListingPrice
from app.services.catalog import RawListing, store_listings, upsert_listing
from collectors.runner import run_source
from tests.sources_helpers import no_sleep

T0 = datetime(2026, 9, 1, 3, 0, tzinfo=UTC)


def raw(pid: str, price: int = 100, *, chain: str = "dia", ean: str | None = None, at: datetime = T0, **kw) -> RawListing:  # type: ignore[no-untyped-def]
    return RawListing(
        chain_id=chain, chain_product_id=pid, name=f"Producto {pid} 1 kg", price_cents=price,
        department="food", ean=ean, observed_at=at, **kw,
    )


def prices(s: Session, pid: str) -> list[tuple[int, datetime, datetime]]:
    listing = s.exec(select(Listing).where(Listing.chain_product_id == pid)).one()
    rows = s.exec(select(ListingPrice).where(ListingPrice.listing_id == listing.id).order_by(col(ListingPrice.id))).all()
    return [(p.price_cents, p.first_seen_at, p.last_seen_at) for p in rows]


def test_new_listings_get_canonical_products_and_prices(clean_db: Engine) -> None:
    with Session(clean_db) as s:
        out = store_listings(s, "easycompra", [raw("a"), raw("b", ean="8480000056733"), raw("c", chain="carrefour", ean="8480000056733")], "")
        s.commit()
        assert out == [(True, True)] * 3
        listings = {li.chain_product_id: li for li in s.exec(select(Listing)).all()}
        assert listings["a"].link_source == "own" and listings["a"].canonical_product_id is not None
        # Same new EAN at two chains in one batch: one shared canonical product.
        assert listings["b"].canonical_product_id == listings["c"].canonical_product_id
        assert listings["b"].link_source == listings["c"].link_source == "ean"
        assert len(s.exec(select(CanonicalProduct)).all()) == 2
        assert listings["a"].quantity_value == 1.0 and listings["a"].quantity_unit == "kg"


def test_prices_are_periods_across_runs(clean_db: Engine) -> None:
    later = T0 + timedelta(days=1)
    with Session(clean_db) as s:
        store_listings(s, "easycompra", [raw("a", 100), raw("b", 100)], "")
        s.commit()
        out = store_listings(s, "easycompra", [raw("a", 100, at=later), raw("b", 120, at=later)], "")
        s.commit()
        assert out == [(False, False), (False, True)]
        assert prices(s, "a") == [(100, T0, later)]  # unchanged: last_seen_at moves
        assert prices(s, "b") == [(100, T0, T0), (120, later, later)]
        # An observation older than what we know is ignored.
        assert store_listings(s, "easycompra", [raw("b", 90, at=T0 - timedelta(days=1))], "") == [(False, False)]


def test_same_product_twice_in_one_batch(clean_db: Engine) -> None:
    later = T0 + timedelta(hours=1)
    with Session(clean_db) as s:
        out = store_listings(s, "easycompra", [raw("a", 100), raw("a", 110, at=later)], "")
        s.commit()
        assert out == [(True, True), (False, True)]  # the second sighting updates the new listing
        assert len(s.exec(select(Listing)).all()) == 1
        assert [p[0] for p in prices(s, "a")] == [100, 110]


def test_existing_listing_gaining_an_ean_is_merged_like_upsert_listing(clean_db: Engine) -> None:
    with Session(clean_db) as s:
        store_listings(s, "easycompra", [raw("x", chain="carrefour", ean="8480000056733"), raw("a")], "")
        s.commit()
        assert len(s.exec(select(CanonicalProduct)).all()) == 2
        store_listings(s, "easycompra", [raw("a", ean="8480000056733", at=T0 + timedelta(days=1))], "")
        s.commit()
        a, x = (s.exec(select(Listing).where(Listing.chain_product_id == pid)).one() for pid in ("a", "x"))
        assert a.canonical_product_id == x.canonical_product_id and a.link_source == "ean"
        assert len(s.exec(select(CanonicalProduct)).all()) == 1  # a's own product was merged away


def test_manual_links_are_left_alone(clean_db: Engine) -> None:
    with Session(clean_db) as s:
        store_listings(s, "easycompra", [raw("a"), raw("b")], "")
        a, b = (s.exec(select(Listing).where(Listing.chain_product_id == pid)).one() for pid in ("a", "b"))
        a.canonical_product_id, a.link_source = b.canonical_product_id, "manual"
        s.add(a)
        s.commit()
        store_listings(s, "easycompra", [raw("a", ean="8480000056733", at=T0 + timedelta(days=1))], "")
        s.commit()
        s.refresh(a)
        assert a.canonical_product_id == b.canonical_product_id and a.link_source == "manual"


def test_batch_matches_one_by_one_results(clean_db: Engine) -> None:
    batch = [raw(str(i), 100 + i, ean="84800000567%02d" % i if i % 3 == 0 else None) for i in range(12)]
    with Session(clean_db) as s:
        store_listings(s, "easycompra", batch, "")
        s.commit()
        batched = sorted(
            (li.chain_product_id, li.ean, li.link_source, li.quantity_value, li.search_text)
            for li in s.exec(select(Listing)).all()
        )
    with Session(clean_db) as s:
        for li in s.exec(select(ListingPrice)).all():
            s.delete(li)
        for li in s.exec(select(Listing)).all():
            s.delete(li)
        for c in s.exec(select(CanonicalProduct)).all():
            s.delete(c)
        s.commit()
        for r in batch:
            upsert_listing(s, "easycompra", r, "")
        s.commit()
        one_by_one = sorted(
            (li.chain_product_id, li.ean, li.link_source, li.quantity_value, li.search_text)
            for li in s.exec(select(Listing)).all()
        )
    assert batched == one_by_one


def test_a_batch_of_200_takes_a_handful_of_round_trips(clean_db: Engine) -> None:
    statements: list[str] = []

    def count(conn, cursor, statement, params, context, executemany):  # type: ignore[no-untyped-def]
        statements.append(statement.split()[0])

    later = T0 + timedelta(days=1)
    with Session(clean_db) as s:
        event.listen(clean_db, "before_cursor_execute", count)
        try:
            store_listings(s, "easycompra", [raw(str(i), ean="848%010d" % i if i % 2 else None) for i in range(200)], "")
            s.commit()
            first = len(statements)
            statements.clear()
            store_listings(s, "easycompra", [raw(str(i), 100 + i % 2, at=later) for i in range(200)], "")
            s.commit()
            second = len(statements)
        finally:
            event.remove(clean_db, "before_cursor_execute", count)
    # One-by-one this was ~2,000 statements per run; round trips to Neon cost ~150 ms each.
    assert first <= 15 and second <= 15, (first, second)


async def test_runner_falls_back_to_one_by_one_when_a_batch_fails(clean_db: Engine) -> None:
    from app.sources.base import PoliteClient
    from tests.test_sources import easycompra_handler

    def handler(request: httpx.Request) -> httpx.Response:
        resp = easycompra_handler(request)
        if request.url.path.endswith("/carrefour.json"):
            items = resp.json()
            items[0]["brand"] = "B" * 300  # longer than the column: this product can't be stored
            return httpx.Response(200, json=items)
        return resp

    client = PoliteClient(transport=httpx.MockTransport(handler), min_interval=0, sleep=no_sleep, retries=0)
    stats = await run_source(clean_db, "easycompra", chains={"carrefour"}, http=client)
    assert stats.products_checked == 2 and stats.errors == 1
    with Session(clean_db) as s:
        assert len(s.exec(select(Listing)).all()) == 2
