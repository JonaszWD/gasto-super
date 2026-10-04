"""Read side of the price comparison. Everything here reads the database only."""

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from math import ceil

from sqlmodel import Session, col, select

from app.models import AppSetting, CanonicalProduct, Chain, Listing, ListingPrice, Purchase, Store, Trip, utcnow
from app.services.catalog import valid_ean
from app.services.matching import find_similar
from app.services.names import clean_name
from app.services.prices import is_stale
from app.services.product_types import ProductType, load_types, types_for_query
from app.services.synonyms import query_groups, relevance, sql_filter
from app.services.text import normalize

# Chains that have a price source. Others (Eroski, Consum...) only appear via "precio pagado".
COMPARED_CHAINS = ["mercadona", "carrefour", "dia", "lidl", "alcampo", "consum"]


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


# ------------------------------------------------------------------ product types


@dataclass
class TypeOffer:
    """One product of a type at one chain, with its latest price."""

    product_id: int
    listing_id: int
    chain_id: str
    name: str
    price_cents: int
    unit_price_cents: int | None
    unit: str | None
    quantity_value: float | None
    last_seen_at: datetime
    stale: bool
    location_specific: bool
    by_weight: bool = False  # sold loose / "aprox": any amount can be bought at the unit price


def _sold_by_weight(listing: Listing) -> bool:
    words = set(normalize(f"{listing.name} {listing.size_text or ''}").split())
    return listing.quantity_unit == "kg" and bool(words & {"aprox", "granel"})


@dataclass
class TypeChain:
    chain_id: str
    chain_name: str
    offers: list[TypeOffer]  # best first
    cheapest: bool = False


@dataclass
class TypeSummary:
    type: ProductType
    products: int
    chains: int
    unit: str | None
    min_unit_price_cents: int | None
    min_chain_id: str | None


def type_offers(session: Session, slugs: list[str], pc: str, stale_days: int = 7) -> dict[str, list[TypeOffer]]:
    """Priced listings of the given types at the compared chains: two queries for any number of types."""
    if not slugs:
        return {}
    rows = session.exec(
        select(Listing, CanonicalProduct.product_type)
        .join(CanonicalProduct, col(CanonicalProduct.id) == Listing.canonical_product_id)
        .where(
            col(CanonicalProduct.product_type).in_(slugs),
            col(Listing.chain_id).in_(COMPARED_CHAINS),
            col(Listing.postal_code).in_(visible_postal_codes(pc)),
        )
    ).all()
    # One listing per product and chain: the location-specific one, else the most recently seen.
    best: dict[tuple[int, str], tuple[Listing, str]] = {}
    for listing, slug in rows:
        key = (listing.canonical_product_id or 0, listing.chain_id)
        cur = best.get(key)
        rank = (listing.postal_code != "", listing.last_seen_at)
        if cur is None or rank > (cur[0].postal_code != "", cur[0].last_seen_at):
            best[key] = (listing, slug or "")
    prices = latest_prices(session, [li.id for li, _ in best.values() if li.id is not None])
    now = utcnow()
    out: dict[str, list[TypeOffer]] = {s: [] for s in slugs}
    for listing, slug in best.values():
        lp = prices.get(listing.id)  # type: ignore[arg-type]
        if lp is None:
            continue
        out[slug].append(
            TypeOffer(
                product_id=listing.canonical_product_id or 0,
                listing_id=listing.id or 0,
                chain_id=listing.chain_id,
                name=clean_name(listing.name, listing.brand),
                price_cents=lp.price_cents,
                unit_price_cents=lp.unit_price_cents,
                unit=listing.quantity_unit,
                quantity_value=listing.quantity_value,
                last_seen_at=lp.last_seen_at,
                stale=is_stale(lp.last_seen_at, now, stale_days),
                location_specific=listing.postal_code != "",
                by_weight=_sold_by_weight(listing),
            )
        )
    return out


def _type_unit(offers: list[TypeOffer]) -> str | None:
    """The unit most of the type's products are priced in (kg for meat, l for milk, unit for eggs)."""
    units = Counter(o.unit for o in offers if o.unit and o.unit_price_cents is not None)
    return units.most_common(1)[0][0] if units else None


def _offer_key(unit: str | None):  # type: ignore[no-untyped-def]
    # Comparable unit prices first, cheapest first; the rest by pack price.
    return lambda o: (not (o.unit == unit and o.unit_price_cents is not None), o.unit_price_cents or 0, o.price_cents)


def type_summaries(session: Session, query: str, pc: str, stale_days: int = 7) -> list[TypeSummary]:
    """Types matching a search, with how many products and the lowest unit price. Empty types are left out."""
    types = types_for_query(query_groups(query))
    offers = type_offers(session, [t.slug for t in types], pc, stale_days)
    out: list[TypeSummary] = []
    for t in types:
        found = offers.get(t.slug, [])
        if not found:
            continue
        unit = _type_unit(found)
        low = min(found, key=_offer_key(unit))
        comparable = low.unit == unit and low.unit_price_cents is not None
        out.append(
            TypeSummary(
                type=t,
                products=len({o.product_id for o in found}),
                chains=len({o.chain_id for o in found}),
                unit=unit,
                min_unit_price_cents=low.unit_price_cents if comparable else None,
                min_chain_id=low.chain_id if comparable else None,
            )
        )
    return out


def type_detail(
    session: Session, slug: str, pc: str, stale_days: int = 7, names: dict[str, str] | None = None
) -> tuple[ProductType, str | None, list[TypeChain]] | None:
    """Every compared chain with its products of this type, cheapest first. None for an unknown type."""
    pt = load_types().get(slug)
    if pt is None:
        return None
    names = names or chain_names(session)
    offers = type_offers(session, [slug], pc, stale_days)[slug]
    unit = _type_unit(offers)
    chains = [
        TypeChain(c, names.get(c, c), sorted([o for o in offers if o.chain_id == c], key=_offer_key(unit)))
        for c in COMPARED_CHAINS
    ]
    # "Cheapest" by unit price, and only with something to compare against (as in _mark_cheapest).
    comparable = [c for c in chains if c.offers and c.offers[0].unit == unit and c.offers[0].unit_price_cents is not None]
    if len(comparable) >= 2:
        min(comparable, key=lambda c: c.offers[0].unit_price_cents or 0).cheapest = True
    # Chains with offers first, the cheapest on top; chains without any at the end.
    by_offer = _offer_key(unit)
    chains.sort(key=lambda c: (not c.offers, by_offer(c.offers[0]) if c.offers else ()))
    return pt, unit, chains


# ------------------------------------------------------------------ shopping list


@dataclass
class ListEntry:
    """One shopping-list item: a specific product (quantity = packs) or a product type (amount in unit)."""

    key: str  # "p:<canonical id>" or "t:<type slug>"
    name: str
    product: CanonicalProduct | None = None
    quantity: int = 1
    product_type: str | None = None
    amount: float | None = None
    unit: str | None = None


@dataclass
class ListLine:
    key: str
    product_id: int | None
    name: str
    quantity: int
    price_cents: int | None
    match: str  # exact | similar | confirmed | none | type
    product_type: str | None = None
    amount: float | None = None
    unit: str | None = None
    # For a type line: the product chosen at this chain and how many packs (None = bought by weight).
    chosen_product_id: int | None = None
    chosen_name: str | None = None
    packs: int | None = None
    stale: bool = False


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
    assignment: dict[str, str]  # line key -> chain_id


MAX_PACKS = 99


def cheapest_for_amount(offers: list[TypeOffer], amount: float, unit: str) -> tuple[TypeOffer, int, int | None] | None:
    """Cheapest way to buy `amount` of a type at one chain: (offer, cost in cents, packs).

    Loose / "aprox" products cost unit price x amount (packs None); packaged ones need whole
    packs (600 g packs for 1 kg means 2 packs). Offers in another unit or without a size can't
    say how much they hold, so they are skipped.
    """
    best: tuple[int, int, TypeOffer, int | None] | None = None
    for o in offers:
        if o.unit != unit:
            continue
        if o.by_weight and o.unit_price_cents is not None:
            cost, packs = round(o.unit_price_cents * amount), None
        elif o.quantity_value:
            packs = max(1, ceil(amount / o.quantity_value - 1e-9))
            if packs > MAX_PACKS:
                continue
            cost = packs * o.price_cents
        else:
            continue
        key = (cost, o.unit_price_cents or 0, o, packs)
        if best is None or key[:2] < best[:2]:
            best = key
    return (best[2], best[0], best[3]) if best else None


def compare_list(
    session: Session, entries: list[ListEntry], pc: str, stale_days: int = 7
) -> tuple[list[ChainTotal], SplitPlan | None]:
    names = chain_names(session)
    cards = {e.key: build_card(session, e.product, pc, stale_days, names) for e in entries if e.product is not None}
    offers = type_offers(session, sorted({e.product_type for e in entries if e.product_type}), pc, stale_days)
    totals: list[ChainTotal] = []
    for chain_id in COMPARED_CHAINS:
        lines, total, missing, similar, stale = [], 0, 0, 0, 0
        for e in entries:
            if e.product is not None:
                assert e.product.id is not None
                row = next(r for r in cards[e.key].prices if r.chain_id == chain_id)
                cost = row.price_cents * e.quantity if row.price_cents is not None else None
                line = ListLine(e.key, e.product.id, e.name, e.quantity, cost, row.match if cost is not None else "none",
                                stale=row.stale)
                similar += cost is not None and row.match in ("similar", "confirmed")
            else:
                at_chain = [o for o in offers.get(e.product_type or "", []) if o.chain_id == chain_id]
                pick = cheapest_for_amount(at_chain, e.amount or 0, e.unit or "") if e.amount and e.unit else None
                line = ListLine(e.key, None, e.name, 1, None, "none", e.product_type, e.amount, e.unit)
                if pick:
                    offer, cost, packs = pick
                    line.price_cents, line.match, line.stale = cost, "type", offer.stale
                    line.chosen_product_id, line.chosen_name, line.packs = offer.product_id, offer.name, packs
            lines.append(line)
            if line.price_cents is None:
                missing += 1
                continue
            total += line.price_cents
            stale += line.stale
        totals.append(ChainTotal(chain_id, names.get(chain_id, chain_id), total, missing, similar, stale, lines))
    totals.sort(key=lambda t: (t.missing, t.total_cents))
    return totals, best_split(totals)


def best_split(totals: list[ChainTotal]) -> SplitPlan | None:
    """Cheapest way to buy the list across two chains (fewest missing items first).

    Only returned when it really uses both chains and beats the best single chain.
    """
    by_chain = {t.chain_id: {ln.key: ln.price_cents for ln in t.lines} for t in totals}
    keys = [ln.key for ln in totals[0].lines] if totals else []
    best: SplitPlan | None = None
    for a, b in combinations(by_chain, 2):
        total, missing, assignment = 0, 0, {}
        for key in keys:
            options = [(p, c) for p, c in ((by_chain[a][key], a), (by_chain[b][key], b)) if p is not None]
            if not options:
                missing += 1
                continue
            price, chain = min(options)
            total += price
            assignment[key] = chain
        if len(set(assignment.values())) < 2:
            continue  # everything at one store is not a split
        plan = SplitPlan((a, b), total, missing, assignment)
        if best is None or (plan.missing, plan.total_cents) < (best.missing, best.total_cents):
            best = plan
    single = totals[0] if totals else None  # totals are sorted by (missing, total)
    if best and single and (best.missing, best.total_cents) >= (single.missing, single.total_cents):
        return None
    return best
