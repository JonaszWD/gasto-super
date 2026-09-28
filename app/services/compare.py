"""Read side of the price comparison. Everything here reads the database only."""

from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations

from sqlmodel import Session, col, select

from app.models import AppSetting, CanonicalProduct, Chain, Listing, ListingPrice, Purchase, Store, Trip, utcnow
from app.services.catalog import valid_ean
from app.services.matching import find_similar
from app.services.names import clean_name
from app.services.prices import is_stale
from app.services.synonyms import query_groups, relevance, sql_filter

# Chains that have a price source. Others (Eroski, Consum...) only appear via "precio pagado".
COMPARED_CHAINS = ["mercadona", "carrefour", "dia", "lidl", "alcampo"]


@dataclass
class ChainPrice:
    chain_id: str
    chain_name: str
    match: str  # exact | confirmed | similar | none
    listing_id: int | None = None
    listing_name: str | None = None
    source: str | None = None
    price_cents: int | None = None
    unit_price_cents: int | None = None
    unit: str | None = None
    quantity_value: float | None = None  # pack size in `unit`
    last_seen_at: datetime | None = None
    stale: bool = False
    cheapest: bool = False
    score: float | None = None
    location_specific: bool = False


@dataclass
class PaidPrice:
    store_name: str
    chain_id: str | None
    unit_price_cents: int
    paid_at: datetime


@dataclass
class ProductCard:
    product: CanonicalProduct
    prices: list[ChainPrice]
    paid: list[PaidPrice] = field(default_factory=list)


def postal_code(session: Session, default: str) -> str:
    row = session.get(AppSetting, "postal_code")
    return row.value if row else default


def visible_postal_codes(pc: str) -> list[str]:
    return [pc, ""]  # "" = source prices that aren't location-specific


def chain_names(session: Session) -> dict[str, str]:
    return {c.id: c.name for c in session.exec(select(Chain)).all()}


def latest_prices(session: Session, listing_ids: list[int]) -> dict[int, ListingPrice]:
    if not listing_ids:
        return {}
    stmt = (
        select(ListingPrice)
        .where(col(ListingPrice.listing_id).in_(listing_ids))
        .order_by(col(ListingPrice.listing_id), col(ListingPrice.last_seen_at).desc(), col(ListingPrice.id).desc())
        .distinct(col(ListingPrice.listing_id))
    )
    return {p.listing_id: p for p in session.exec(stmt).all()}


def _best_exact(session: Session, product_id: int, chain_id: str, pcs: list[str]) -> Listing | None:
    # Prefer the location-specific listing over a generic one.
    listings = session.exec(
        select(Listing).where(
            Listing.canonical_product_id == product_id,
            Listing.chain_id == chain_id,
            col(Listing.postal_code).in_(pcs),
        )
    ).all()
    return sorted(listings, key=lambda li: (li.postal_code == "", -li.last_seen_at.timestamp()))[0] if listings else None


def build_card(
    session: Session, product: CanonicalProduct, pc: str, stale_days: int = 7, names: dict[str, str] | None = None
) -> ProductCard:
    assert product.id is not None
    names = names or chain_names(session)
    pcs = visible_postal_codes(pc)
    now = utcnow()
    rows: list[ChainPrice] = []
    chosen: list[tuple[ChainPrice, Listing]] = []
    for chain_id in COMPARED_CHAINS:
        exact = _best_exact(session, product.id, chain_id, pcs)
        if exact is not None:
            row = ChainPrice(chain_id, names.get(chain_id, chain_id), "exact")
            chosen.append((row, exact))
        else:
            cand = find_similar(session, product, chain_id, pcs)
            if cand is None:
                rows.append(ChainPrice(chain_id, names.get(chain_id, chain_id), "none"))
                continue
            row = ChainPrice(chain_id, names.get(chain_id, chain_id), cand.status, score=cand.score)
            chosen.append((row, cand.listing))
        rows.append(row)

    prices = latest_prices(session, [li.id for _, li in chosen if li.id is not None])
    for row, listing in chosen:
        lp = prices.get(listing.id)  # type: ignore[arg-type]
        row.listing_id = listing.id
        row.listing_name = clean_name(listing.name, listing.brand)
        row.quantity_value = listing.quantity_value
        row.source = listing.source
        row.unit = listing.quantity_unit
        row.location_specific = listing.postal_code != ""
        if lp is None:  # listing known but never priced
            continue
        row.price_cents = lp.price_cents
        row.unit_price_cents = lp.unit_price_cents
        row.last_seen_at = lp.last_seen_at
        row.stale = is_stale(lp.last_seen_at, now, stale_days)

    _mark_cheapest(rows)
    return ProductCard(product, rows, paid_prices(session, product))


def _mark_cheapest(rows: list[ChainPrice]) -> None:
    """Cheapest by unit price (only across rows in the same unit); package price as fallback."""
    priced = [r for r in rows if r.price_cents is not None]
    if len(priced) < 2:  # "cheapest" only means something with something to compare against
        return
    with_unit = [r for r in priced if r.unit_price_cents is not None and r.unit]
    units = {r.unit for r in with_unit}
    if with_unit and len(units) == 1:
        best = min(with_unit, key=lambda r: r.unit_price_cents or 0)
    else:
        best = min(priced, key=lambda r: r.price_cents or 0)
    best.cheapest = True


def paid_prices(session: Session, product: CanonicalProduct) -> list[PaidPrice]:
    """Latest price I paid per store, from the spending tracker ("precio pagado")."""
    if not product.ean:
        return []
    keys = {product.ean, product.ean.lstrip("0"), product.ean[1:] if product.ean.startswith("0") else product.ean}
    stmt = (
        select(Purchase, Store)
        .join(Trip, col(Trip.id) == Purchase.trip_id)
        .join(Store, col(Store.id) == Trip.store_id)
        .where(col(Purchase.product_key).in_(keys))
        .order_by(col(Purchase.created_at).desc())
    )
    seen: set[int] = set()
    out: list[PaidPrice] = []
    for purchase, store in session.exec(stmt).all():
        if store.id in seen:
            continue
        seen.add(store.id)  # type: ignore[arg-type]
        out.append(PaidPrice(store.name, store.chain_id, purchase.unit_price_cents, purchase.created_at))
    return out


def search_products(session: Session, query: str, limit: int = 10) -> list[CanonicalProduct]:
    q = query.strip()
    digits = "".join(ch for ch in q if ch.isdigit())
    if digits and len(digits) == len(q.replace(" ", "")) and len(digits) >= 8:
        ean = valid_ean(digits)
        if ean:
            found = session.exec(select(CanonicalProduct).where(CanonicalProduct.ean == ean)).first()
            return [found] if found else []
    # English or Spanish: "milk" finds "Leche entera" (see services/synonyms.py).
    groups = query_groups(q)
    if not groups:
        return []
    stmt = select(CanonicalProduct).where(sql_filter(col(CanonicalProduct.search_text))(groups))
    candidates = session.exec(stmt.limit(1000)).all()
    # Most relevant first (see synonyms.relevance); ties: products with an EAN, then shorter names.
    scored = [(relevance(p.name, p.brand, groups), p) for p in candidates]
    scored.sort(key=lambda sp: (-sp[0], sp[1].ean is None, len(sp[1].name), sp[1].name))
    return [p for _, p in scored[:limit]]


# ------------------------------------------------------------------ shopping list


@dataclass
class ListLine:
    product_id: int
    name: str
    quantity: int
    price_cents: int | None
    match: str


@dataclass
class ChainTotal:
    chain_id: str
    chain_name: str
    total_cents: int
    missing: int
    similar: int
    stale: int
    lines: list[ListLine]


@dataclass
class SplitPlan:
    chain_ids: tuple[str, str]
    total_cents: int
    missing: int
    assignment: dict[int, str]  # product_id -> chain_id


def compare_list(
    session: Session, items: list[tuple[CanonicalProduct, int]], pc: str, stale_days: int = 7
) -> tuple[list[ChainTotal], SplitPlan | None]:
    names = chain_names(session)
    cards = [(build_card(session, p, pc, stale_days, names), qty) for p, qty in items]
    totals: list[ChainTotal] = []
    for chain_id in COMPARED_CHAINS:
        lines, total, missing, similar, stale = [], 0, 0, 0, 0
        for card, qty in cards:
            row = next(r for r in card.prices if r.chain_id == chain_id)
            assert card.product.id is not None
            if row.price_cents is None:
                missing += 1
                lines.append(ListLine(card.product.id, clean_name(card.product.name, card.product.brand), qty, None, "none"))
                continue
            total += row.price_cents * qty
            similar += row.match in ("similar", "confirmed")
            stale += row.stale
            lines.append(ListLine(card.product.id, clean_name(card.product.name, card.product.brand), qty, row.price_cents * qty, row.match))
        totals.append(ChainTotal(chain_id, names.get(chain_id, chain_id), total, missing, similar, stale, lines))
    totals.sort(key=lambda t: (t.missing, t.total_cents))
    return totals, best_split(totals)


def best_split(totals: list[ChainTotal]) -> SplitPlan | None:
    """Cheapest way to buy the list across two chains (fewest missing items first).

    Only returned when it really uses both chains and beats the best single chain.
    """
    by_chain = {t.chain_id: {ln.product_id: ln.price_cents for ln in t.lines} for t in totals}
    product_ids = [ln.product_id for ln in totals[0].lines] if totals else []
    best: SplitPlan | None = None
    for a, b in combinations(by_chain, 2):
        total, missing, assignment = 0, 0, {}
        for pid in product_ids:
            options = [(p, c) for p, c in ((by_chain[a][pid], a), (by_chain[b][pid], b)) if p is not None]
            if not options:
                missing += 1
                continue
            price, chain = min(options)
            total += price
            assignment[pid] = chain
        if len(set(assignment.values())) < 2:
            continue  # everything at one store is not a split
        plan = SplitPlan((a, b), total, missing, assignment)
        if best is None or (plan.missing, plan.total_cents) < (best.missing, best.total_cents):
            best = plan
    single = totals[0] if totals else None  # totals are sorted by (missing, total)
    if best and single and (best.missing, best.total_cents) >= (single.missing, single.total_cents):
        return None
    return best
