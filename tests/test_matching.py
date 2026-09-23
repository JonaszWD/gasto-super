"""Exact (EAN) and similar matching, and manual overrides."""

from sqlalchemy import Engine
from sqlmodel import Session, select

from app.models import CanonicalProduct, Listing, ProductMatch, ShoppingListItem
from app.services.catalog import RawListing, upsert_listing, valid_ean
from app.services.matching import find_similar, keywords, score
from tests.compare_helpers import add_listing

EAN = "8410500028480"


def test_same_ean_links_to_one_canonical_product(clean_db: Engine) -> None:
    with Session(clean_db) as s:
        a = add_listing(s, "carrefour", "c1", "Actimel fresa 6 x 100 g", 419, ean=EAN)
        b = add_listing(s, "dia", "d1", "ACTIMEL Fresa pack 6", 399, ean=EAN)
        c = add_listing(s, "mercadona", "m1", "Actimel fresa", 405, ean=EAN)
        assert a.canonical_product_id == b.canonical_product_id == c.canonical_product_id
        assert a.link_source == "ean"
        assert len(s.exec(select(CanonicalProduct)).all()) == 1


def test_listing_without_ean_gets_own_product_then_merges_when_ean_arrives(clean_db: Engine) -> None:
    with Session(clean_db) as s:
        ean_listing = add_listing(s, "carrefour", "c1", "Leche entera 1 L", 99, ean="8480000123459")
        merc = add_listing(s, "mercadona", "m1", "Leche entera Hacendado 1 L", 95)
        own_id = merc.canonical_product_id
        assert own_id != ean_listing.canonical_product_id and merc.link_source == "own"
        s.add(ShoppingListItem(canonical_product_id=own_id, quantity=2))  # type: ignore[arg-type]
        s.commit()

        # Nightly EAN backfill: product detail brings the EAN.
        raw = RawListing(chain_id="mercadona", chain_product_id="m1", name="Leche entera Hacendado 1 L",
                         price_cents=95, department="food", ean="8480000123459")
        merged, _ = upsert_listing(s, "mercadona", raw, "28020")
        s.commit()
        assert merged.canonical_product_id == ean_listing.canonical_product_id
        assert s.get(CanonicalProduct, own_id) is None  # orphan merged away
        item = s.exec(select(ShoppingListItem)).one()
        assert item.canonical_product_id == ean_listing.canonical_product_id  # shopping list followed


def test_first_ean_upgrades_own_product_in_place(clean_db: Engine) -> None:
    with Session(clean_db) as s:
        merc = add_listing(s, "mercadona", "m1", "Aceite de oliva 1 L", 370)
        own_id = merc.canonical_product_id
        raw = RawListing(chain_id="mercadona", chain_product_id="m1", name="Aceite de oliva 1 L",
                         price_cents=370, department="food", ean="8402001027482")
        listing, _ = upsert_listing(s, "mercadona", raw, "28020")
        assert listing.canonical_product_id == own_id
        assert s.get(CanonicalProduct, own_id).ean == "8402001027482"  # type: ignore[union-attr]


def test_manual_link_is_never_overridden(clean_db: Engine) -> None:
    with Session(clean_db) as s:
        target = add_listing(s, "carrefour", "c1", "Leche entera 1 L", 99, ean="8480000123459")
        dia = add_listing(s, "dia", "d1", "Leche entera Dia 1 L", 89)
        dia.canonical_product_id, dia.link_source = target.canonical_product_id, "manual"
        s.add(dia)
        s.commit()
        raw = RawListing(chain_id="dia", chain_product_id="d1", name="Leche entera Dia 1 L", price_cents=89,
                         department="food", ean="8480017240804")
        again, _ = upsert_listing(s, "easycompra", raw, "")
        assert again.canonical_product_id == target.canonical_product_id and again.link_source == "manual"


def test_valid_ean() -> None:
    assert valid_ean("8480000123459") == "8480000123459"
    assert valid_ean("036000291452") == "0036000291452"  # UPC-A -> EAN-13
    assert valid_ean("123") is None
    assert valid_ean(None) is None


def test_keywords_ignore_store_brands_and_sizes() -> None:
    assert keywords("Leche entera Hacendado 6 x 1 L") == {"leche", "entera"}
    assert keywords("Aceite de oliva virgen extra Dia 1 L") == {"aceite", "oliva", "virgen", "extra"}
    assert keywords("Yogur griego Milbona", brand="Milbona") == {"yogur", "griego"}


def test_similar_match_across_store_brands(clean_db: Engine) -> None:
    with Session(clean_db) as s:
        merc = add_listing(s, "mercadona", "m1", "Leche entera Hacendado", 95, size="Brick 1 l", brand="Hacendado")
        add_listing(s, "dia", "d1", "Leche entera Dia Láctea 1 L", 89, brand="Dia Láctea")
        add_listing(s, "dia", "d2", "Leche semidesnatada Dia 1 L", 85)
        add_listing(s, "dia", "d3", "Leche entera Dia 6 x 1 L", 510)  # size not comparable
        add_listing(s, "dia", "d4", "Detergente leche 1 L", 300, department="household")
        product = s.get(CanonicalProduct, merc.canonical_product_id)
        assert product
        cand = find_similar(s, product, "dia", ["28020", ""])
        assert cand is not None
        assert cand.listing.chain_product_id == "d1"
        assert cand.status == "similar"


def test_similar_requires_comparable_size() -> None:
    p = CanonicalProduct(name="Aceite de oliva", department="food", quantity_value=1.0, quantity_unit="l")
    near = Listing(name="Aceite de oliva", department="food", quantity_value=0.9, quantity_unit="l",
                   source="x", chain_id="dia", chain_product_id="1")
    far = Listing(name="Aceite de oliva", department="food", quantity_value=5.0, quantity_unit="l",
                  source="x", chain_id="dia", chain_product_id="2")
    other_unit = Listing(name="Aceite de oliva", department="food", quantity_value=1.0, quantity_unit="kg",
                         source="x", chain_id="dia", chain_product_id="3")
    assert score(p, near) > 0.8
    assert score(p, far) == 0
    assert score(p, other_unit) == 0


def test_manual_reject_and_confirm_take_priority(clean_db: Engine) -> None:
    with Session(clean_db) as s:
        merc = add_listing(s, "mercadona", "m1", "Leche entera Hacendado 1 L", 95)
        best = add_listing(s, "dia", "d1", "Leche entera Dia 1 L", 89)
        other = add_listing(s, "dia", "d2", "Leche entera de pastoreo 1 L", 129)
        product = s.get(CanonicalProduct, merc.canonical_product_id)
        assert product and product.id
        assert find_similar(s, product, "dia", ["28020", ""]).listing.id == best.id  # type: ignore[union-attr]

        s.add(ProductMatch(canonical_product_id=product.id, listing_id=best.id, status="rejected"))  # type: ignore[arg-type]
        s.commit()
        assert find_similar(s, product, "dia", ["28020", ""]).listing.id == other.id  # type: ignore[union-attr]

        s.add(ProductMatch(canonical_product_id=product.id, listing_id=other.id, status="confirmed"))  # type: ignore[arg-type]
        s.commit()
        cand = find_similar(s, product, "dia", ["28020", ""])
        assert cand and cand.status == "confirmed" and cand.listing.id == other.id
