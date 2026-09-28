"""Similar-product matching across chains (for products without a shared EAN).

A candidate at another chain must be in the same department and base unit, have a comparable
size and share keywords. Brands (especially store brands) are ignored, because the point is to
find e.g. Hacendado milk's equivalent at Dia. Manual decisions (ProductMatch) always win.
"""

from dataclasses import dataclass

from sqlalchemy import or_
from sqlmodel import Session, col, select

from app.models import CanonicalProduct, Listing, ProductMatch
from app.services.names import clean_name
from app.services.text import normalize

STOPWORDS = {
    "de", "del", "la", "el", "los", "las", "con", "y", "en", "para", "al", "a", "sin", "o", "e",
    "pack", "paquete", "bandeja", "bote", "botella", "brick", "lata", "latas", "tarro", "bolsa",
    "garrafa", "caja", "frasco", "envase", "tarrina", "malla", "x", "g", "gr", "kg", "l", "ml",
    "cl", "ud", "uds", "unidades", "aprox", "formato", "familiar", "ahorro",
}
# Store-brand names only; generic words like "extra" or "verde" carry meaning and stay.
STORE_BRANDS = {
    "hacendado", "deliplus", "compy", "dia", "carrefour", "alcampo", "auchan", "eroski", "consum",
    "lidl", "milbona", "aldi", "milsani", "ahorramas", "alipende", "belbake", "solevita", "pikok",
}
MIN_SCORE = 0.45
MIN_SIZE_RATIO = 0.5  # cards compare unit price, so pack size only rules out very different formats


def keywords(name: str, brand: str | None = None) -> set[str]:
    brand_tokens = set(normalize(brand).split()) if brand else set()
    return {
        t
        for t in normalize(clean_name(name, brand)).split()
        if len(t) > 1 and not t.isdigit() and t not in STOPWORDS and t not in STORE_BRANDS and t not in brand_tokens
    }


@dataclass(frozen=True)
class Candidate:
    listing: Listing
    score: float
    status: str  # "similar" | "confirmed"


def _size_ratio(a_value: float | None, a_unit: str | None, b_value: float | None, b_unit: str | None) -> float | None:
    if not a_value or not b_value or not a_unit or a_unit != b_unit:
        return None
    return min(a_value, b_value) / max(a_value, b_value)


def score(product: CanonicalProduct, listing: Listing) -> float:
    if product.department != listing.department:
        return 0.0
    if product.quantity_unit and listing.quantity_unit and product.quantity_unit != listing.quantity_unit:
        return 0.0
    a = keywords(product.name, product.brand)
    b = keywords(listing.name, listing.brand)
    if not a or not b:
        return 0.0
    jaccard = len(a & b) / len(a | b)
    if jaccard == 0:
        return 0.0
    s = 0.7 * jaccard
    ratio = _size_ratio(product.quantity_value, product.quantity_unit, listing.quantity_value, listing.quantity_unit)
    if ratio is not None:
        if ratio < MIN_SIZE_RATIO:
            return 0.0
        s += 0.2 * ratio
    if product.category and listing.category and keywords(product.category) & keywords(listing.category):
        s += 0.1
    return round(s, 3)


def find_similar(
    session: Session, product: CanonicalProduct, chain_id: str, postal_codes: list[str], limit: int = 200
) -> Candidate | None:
    """Best equivalent of `product` at `chain_id`, honouring manual confirm/reject decisions."""
    assert product.id is not None
    decisions = {
        m.listing_id: m.status
        for m in session.exec(select(ProductMatch).where(ProductMatch.canonical_product_id == product.id)).all()
    }
    base = select(Listing).where(
        Listing.chain_id == chain_id,
        col(Listing.postal_code).in_(postal_codes),
        col(Listing.canonical_product_id).is_distinct_from(product.id),
    )
    confirmed_ids = [lid for lid, status in decisions.items() if status == "confirmed"]
    if confirmed_ids:
        confirmed = session.exec(base.where(col(Listing.id).in_(confirmed_ids))).first()
        if confirmed:
            return Candidate(confirmed, 1.0, "confirmed")

    words = sorted(keywords(product.name, product.brand), key=len, reverse=True)[:3]
    if not words:
        return None
    stmt = base.where(
        Listing.department == product.department,
        or_(*[col(Listing.search_text).contains(w) for w in words]),
    ).limit(limit)
    best: Candidate | None = None
    for listing in session.exec(stmt).all():
        if decisions.get(listing.id) == "rejected":
            continue
        s = score(product, listing)
        if s >= MIN_SCORE and (best is None or s > best.score):
            best = Candidate(listing, s, "similar")
    return best
