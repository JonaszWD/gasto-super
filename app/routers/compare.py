import asyncio

import httpx
from fastapi import APIRouter, HTTPException, Query
from sqlmodel import Session, col, delete, select

from app.config import Settings
from app.deps import SessionDep, SettingsDep
from app.models import AppSetting, CanonicalProduct, Listing, ListingPrice, ProductMatch, Store
from app.schemas import (
    Alternative,
    AlternativeOut,
    ChainHistory,
    ChainPriceOut,
    ComparedProduct,
    HistoryPoint,
    MatchAction,
    PaidPriceOut,
    ProductCardOut,
    ProductDetailOut,
    SearchOut,
    SettingsIn,
    SettingsOut,
)
from app.services.barcode_parser import normalize as normalize_barcode
from app.services.catalog import link_listing, record_listing_price, upsert_listing, valid_ean
from app.services.compare import ProductCard, build_card, chain_names, postal_code, search_products
from app.services.kv import DbCache
from app.services.names import clean_name
from app.services.quantity import Quantity, unit_price_cents
from app.sources.base import PoliteClient, SourceContext, SourceError
from app.sources.registry import build_adapter, enabled_sources

router = APIRouter(prefix="/api", tags=["compare"])


def _image(session: Session, product_id: int) -> str | None:
    return session.exec(
        select(Listing.image_url).where(Listing.canonical_product_id == product_id, col(Listing.image_url).is_not(None))
    ).first()


def card_out(session: Session, card: ProductCard) -> ProductCardOut:
    live = {s for s in enabled_sources() if (a := build_adapter(s)) and a.supports_live_refresh}
    p = card.product
    assert p.id is not None
    return ProductCardOut(
        product=ComparedProduct.model_validate(p, from_attributes=True).model_copy(
            update={"name": clean_name(p.name, p.brand)}
        ),
        prices=[
            ChainPriceOut(**{**r.__dict__, "can_refresh": r.source in live and r.listing_id is not None})
            for r in card.prices
        ],
        paid=[PaidPriceOut(**pp.__dict__) for pp in card.paid],
        image_url=_image(session, p.id),
    )


def _product_or_404(session: Session, product_id: int) -> CanonicalProduct:
    product = session.get(CanonicalProduct, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product_not_found")
    return product


@router.get("/compare/search")
def search(session: SessionDep, settings: SettingsDep, q: str = Query(min_length=1, max_length=100)) -> SearchOut:
    pc = postal_code(session, settings.default_postal_code)
    names = chain_names(session)
    cards = [build_card(session, p, pc, settings.stale_after_days, names) for p in search_products(session, q, limit=8)]
    return SearchOut(query=q, postal_code=pc, results=[card_out(session, c) for c in cards])


def _detail(session: Session, settings: Settings, product: CanonicalProduct) -> ProductDetailOut:
    pc = postal_code(session, settings.default_postal_code)
    card = build_card(session, product, pc, settings.stale_after_days)
    base = card_out(session, card)
    history = []
    for row in card.prices:
        if row.listing_id is None:
            continue
        points = session.exec(
            select(ListingPrice).where(ListingPrice.listing_id == row.listing_id).order_by(col(ListingPrice.first_seen_at))
        ).all()
        history.append(
            ChainHistory(
                chain_id=row.chain_id,
                chain_name=row.chain_name,
                listing_id=row.listing_id,
                listing_name=row.listing_name or "",
                match=row.match,
                source=row.source or "",
                points=[HistoryPoint.model_validate(pt, from_attributes=True) for pt in points],
            )
        )
    return ProductDetailOut(**base.model_dump(), history=history)


@router.get("/compare/products/{product_id}")
def product_detail(product_id: int, session: SessionDep, settings: SettingsDep) -> ProductDetailOut:
    return _detail(session, settings, _product_or_404(session, product_id))


@router.post("/compare/products/{product_id}/matches")
def set_match(product_id: int, body: MatchAction, session: SessionDep, settings: SettingsDep) -> ProductDetailOut:
    """Manual matching. confirm/reject a similar listing, relink it as the same product, or reset."""
    product = _product_or_404(session, product_id)
    listing = session.get(Listing, body.listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail="listing_not_found")
    session.exec(
        delete(ProductMatch).where(
            col(ProductMatch.canonical_product_id) == product_id, col(ProductMatch.listing_id) == listing.id
        )
    )
    match body.action:
        case "confirm" | "reject":
            status = "confirmed" if body.action == "confirm" else "rejected"
            session.add(ProductMatch(canonical_product_id=product_id, listing_id=listing.id, status=status))  # type: ignore[arg-type]
        case "relink":
            listing.canonical_product_id = product_id
            listing.link_source = "manual"
            session.add(listing)
        case "reset":
            if listing.link_source == "manual":
                listing.link_source = "own"
                listing.canonical_product_id = None
                session.add(listing)
                session.flush()
                link_listing(session, listing)
    session.commit()
    session.refresh(product)
    return _detail(session, settings, product)


@router.post("/compare/listings/{listing_id}/refresh")
async def refresh_listing(listing_id: int, session: SessionDep, settings: SettingsDep) -> ProductCardOut:
    """Re-fetch one listing live. Strict timeout; nothing continues after the response."""
    listing = session.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail="listing_not_found")
    adapter = build_adapter(listing.source)
    if adapter is None or not adapter.supports_live_refresh or listing.source not in enabled_sources():
        raise HTTPException(status_code=409, detail="refresh_not_supported")
    timeout = settings.refresh_timeout_seconds
    client = PoliteClient(
        user_agent=adapter.user_agent or settings.sources_user_agent, min_interval=0, timeout=timeout, retries=0
    )
    ctx = SourceContext(
        postal_code=listing.postal_code or postal_code(session, settings.default_postal_code),
        http=client,
        cache=DbCache(session),
    )
    try:
        raw = await asyncio.wait_for(adapter.fetch_product(ctx, listing.chain_product_id), timeout=timeout)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="refresh_timeout") from exc
    except (SourceError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail="refresh_failed") from exc
    finally:
        await client.aclose()
    if raw is None:
        raise HTTPException(status_code=404, detail="listing_gone")
    updated, _ = upsert_listing(session, listing.source, raw, listing.postal_code)
    record_listing_price(session, updated, raw)
    session.commit()
    product = _product_or_404(session, updated.canonical_product_id or 0)
    pc = postal_code(session, settings.default_postal_code)
    return card_out(session, build_card(session, product, pc, settings.stale_after_days))


@router.get("/compare/alternative")
def alternative(
    session: SessionDep,
    settings: SettingsDep,
    barcode: str = Query(max_length=64),
    store_id: int | None = None,
    price_cents: int | None = Query(default=None, ge=0),
) -> AlternativeOut:
    """"¿Más barato en otro sitio?" for a scanned product, against the price I'm about to pay."""
    ean = valid_ean(normalize_barcode(barcode))
    product = session.exec(select(CanonicalProduct).where(CanonicalProduct.ean == ean)).first() if ean else None
    if product is None:
        return AlternativeOut(alternative=None)
    pc = postal_code(session, settings.default_postal_code)
    card = build_card(session, product, pc, settings.stale_after_days)
    store = session.get(Store, store_id) if store_id else None
    current_chain = store.chain_id if store else None

    ref_price, ref_label = price_cents, "entered"  # translated by the UI
    if ref_price is None and current_chain:
        own = next((r for r in card.prices if r.chain_id == current_chain and r.match == "exact"), None)
        if own and own.price_cents is not None:
            ref_price, ref_label = own.price_cents, own.chain_name
    if ref_price is None:
        return AlternativeOut(alternative=None)
    qty = Quantity(product.quantity_value, product.quantity_unit) if product.quantity_value and product.quantity_unit else None
    ref_unit = unit_price_cents(ref_price, qty)

    best: tuple[int, Alternative] | None = None
    for row in card.prices:
        if row.chain_id == current_chain or row.price_cents is None or row.stale or row.match == "none":
            continue
        if ref_unit is not None and row.unit_price_cents is not None and qty and row.unit == qty.unit:
            savings = round((ref_unit - row.unit_price_cents) * qty.value)
        elif row.match == "exact":
            savings = ref_price - row.price_cents
        else:
            continue
        if savings <= 0:
            continue
        alt = Alternative(
            product_id=product.id,  # type: ignore[arg-type]
            chain_id=row.chain_id,
            chain_name=row.chain_name,
            price_cents=row.price_cents,
            unit_price_cents=row.unit_price_cents,
            unit=row.unit,
            match=row.match,
            reference_price_cents=ref_price,
            reference_label=ref_label,
            savings_cents=savings,
            last_seen_at=row.last_seen_at,
        )
        if best is None or savings > best[0]:
            best = (savings, alt)
    return AlternativeOut(alternative=best[1] if best else None)


def _language(session: Session) -> str:
    row = session.get(AppSetting, "language")
    return row.value if row and row.value in ("es", "en") else "es"


@router.get("/settings")
def get_app_settings(session: SessionDep, settings: SettingsDep) -> SettingsOut:
    return SettingsOut(postal_code=postal_code(session, settings.default_postal_code), language=_language(session))  # type: ignore[arg-type]


@router.put("/settings")
def put_app_settings(body: SettingsIn, session: SessionDep, settings: SettingsDep) -> SettingsOut:
    for key, value in (("postal_code", body.postal_code), ("language", body.language)):
        if value is None:
            continue
        row = session.get(AppSetting, key) or AppSetting(key=key, value=value)
        row.value = value
        session.add(row)
    session.commit()
    return get_app_settings(session, settings)
