from typing import Annotated

from fastapi import Depends, HTTPException
from sqlmodel import Session, func, select

from app.config import Settings, get_settings
from app.db import get_session
from app.models import Purchase, Store, Trip
from app.services.barcode_parser import StoreRules, load_rules_cached, store_rules_key
from app.services.openfoodfacts import OpenFoodFactsClient

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_off_client(settings: SettingsDep) -> OpenFoodFactsClient:
    return OpenFoodFactsClient(settings)


def get_rules(settings: SettingsDep) -> StoreRules:
    return load_rules_cached(settings.store_rules_path)


OffClientDep = Annotated[OpenFoodFactsClient, Depends(get_off_client)]
RulesDep = Annotated[StoreRules, Depends(get_rules)]


def rules_key_for(store: Store | None) -> str | None:
    if store is None:
        return None
    return store.rules_key or store_rules_key(store.name)


def get_trip_or_404(session: Session, trip_id: int) -> Trip:
    trip = session.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="trip_not_found")
    return trip


def last_unit_price(session: Session, product_key: str) -> int | None:
    stmt = (
        select(Purchase.unit_price_cents)
        .where(Purchase.product_key == product_key)
        .order_by(Purchase.created_at.desc(), Purchase.id.desc())  # type: ignore[union-attr]
        .limit(1)
    )
    return session.exec(stmt).first()


def trip_totals(session: Session, trip_ids: list[int]) -> dict[int, tuple[int, int]]:
    """trip_id -> (total_cents, item_count)."""
    if not trip_ids:
        return {}
    stmt = (
        select(Purchase.trip_id, func.coalesce(func.sum(Purchase.total_cents), 0), func.count(Purchase.id))
        .where(Purchase.trip_id.in_(trip_ids))  # type: ignore[attr-defined]
        .group_by(Purchase.trip_id)
    )
    return {tid: (int(total), int(count)) for tid, total, count in session.exec(stmt).all()}
