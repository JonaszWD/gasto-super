from fastapi import APIRouter, HTTPException, Query
from sqlmodel import Session, col, select

from app.deps import RulesDep, SessionDep, get_trip_or_404, rules_key_for, trip_totals
from app.models import Product, Purchase, Store, Trip, utcnow
from app.routers.products import product_from_catalog
from app.schemas import PurchaseCreate, PurchaseOut, PurchaseUpdate, TripCreate, TripDetail, TripOut
from app.services.barcode_parser import parse_barcode
from app.services.money import line_total_cents

router = APIRouter(prefix="/api", tags=["trips"])


def _open_trip(session: Session) -> Trip | None:
    return session.exec(select(Trip).where(col(Trip.closed_at).is_(None)).order_by(col(Trip.id).desc())).first()


def _trip_out(session: Session, trip: Trip) -> TripOut:
    assert trip.id is not None
    store = session.get(Store, trip.store_id)
    total, count = trip_totals(session, [trip.id]).get(trip.id, (0, 0))
    return TripOut(
        id=trip.id,
        store_id=trip.store_id,
        store_name=store.name if store else "?",
        started_at=trip.started_at,
        closed_at=trip.closed_at,
        total_cents=total,
        item_count=count,
    )


def _trip_detail(session: Session, trip: Trip) -> TripDetail:
    purchases = session.exec(
        select(Purchase).where(Purchase.trip_id == trip.id).order_by(col(Purchase.created_at).desc(), col(Purchase.id).desc())
    ).all()
    return TripDetail(
        **_trip_out(session, trip).model_dump(),
        purchases=[PurchaseOut.model_validate(p) for p in purchases],
    )


@router.get("/trips")
def list_trips(session: SessionDep, limit: int = Query(default=100, ge=1, le=1000)) -> list[TripOut]:
    trips = session.exec(select(Trip).order_by(col(Trip.started_at).desc()).limit(limit)).all()
    ids = [t.id for t in trips if t.id is not None]
    totals = trip_totals(session, ids)
    stores = {s.id: s.name for s in session.exec(select(Store)).all()}
    return [
        TripOut(
            id=t.id,
            store_id=t.store_id,
            store_name=stores.get(t.store_id, "?"),
            started_at=t.started_at,
            closed_at=t.closed_at,
            total_cents=totals.get(t.id, (0, 0))[0],
            item_count=totals.get(t.id, (0, 0))[1],
        )
        for t in trips
        if t.id is not None
    ]


@router.get("/trips/current")
def current_trip(session: SessionDep) -> TripDetail | None:
    trip = _open_trip(session)
    return _trip_detail(session, trip) if trip else None


@router.post("/trips", status_code=201)
def start_trip(body: TripCreate, session: SessionDep) -> TripDetail:
    if session.get(Store, body.store_id) is None:
        raise HTTPException(status_code=404, detail="store_not_found")
    if _open_trip(session) is not None:
        raise HTTPException(status_code=409, detail="trip_already_open")
    trip = Trip(store_id=body.store_id)
    session.add(trip)
    session.commit()
    session.refresh(trip)
    return _trip_detail(session, trip)


@router.get("/trips/{trip_id}")
def get_trip(trip_id: int, session: SessionDep) -> TripDetail:
    return _trip_detail(session, get_trip_or_404(session, trip_id))


@router.post("/trips/{trip_id}/close")
def close_trip(trip_id: int, session: SessionDep) -> TripOut:
    trip = get_trip_or_404(session, trip_id)
    if trip.closed_at is None:
        trip.closed_at = utcnow()
        session.add(trip)
        session.commit()
        session.refresh(trip)
    return _trip_out(session, trip)


@router.post("/trips/{trip_id}/reopen")
def reopen_trip(trip_id: int, session: SessionDep) -> TripDetail:
    trip = get_trip_or_404(session, trip_id)
    other = _open_trip(session)
    if other is not None and other.id != trip.id:
        raise HTTPException(status_code=409, detail="trip_already_open")
    trip.closed_at = None
    session.add(trip)
    session.commit()
    session.refresh(trip)
    return _trip_detail(session, trip)


@router.delete("/trips/{trip_id}", status_code=204)
def delete_trip(trip_id: int, session: SessionDep) -> None:
    trip = get_trip_or_404(session, trip_id)
    for p in session.exec(select(Purchase).where(Purchase.trip_id == trip_id)).all():
        session.delete(p)
    session.delete(trip)
    session.commit()


@router.post("/trips/{trip_id}/purchases", status_code=201)
def add_purchase(trip_id: int, body: PurchaseCreate, session: SessionDep, rules: RulesDep) -> PurchaseOut:
    trip = get_trip_or_404(session, trip_id)
    parsed = parse_barcode(body.barcode, rules, rules_key_for(session.get(Store, trip.store_id)))
    if not parsed.code:
        raise HTTPException(status_code=422, detail="invalid_barcode")

    product = session.get(Product, parsed.product_key) or product_from_catalog(session, parsed.product_key)
    if product is None:
        if not body.name:
            raise HTTPException(status_code=422, detail="name_required")
        # Remember manually named products (store brands, weighed items) for next time.
        product = Product(key=parsed.product_key, name=body.name.strip(), category=body.category, source="manual")
        session.add(product)

    purchase = Purchase(
        trip_id=trip_id,
        barcode=parsed.code,
        product_key=parsed.product_key,
        name=product.name,
        category=body.category if body.category is not None else product.category,
        unit_price_cents=body.unit_price,
        quantity=body.quantity,
        total_cents=line_total_cents(body.unit_price, body.quantity),
        weight_grams=body.weight_grams if body.weight_grams is not None else parsed.weight_grams,
    )
    session.add(purchase)
    session.commit()
    session.refresh(purchase)
    return PurchaseOut.model_validate(purchase)


@router.patch("/purchases/{purchase_id}")
def update_purchase(purchase_id: int, body: PurchaseUpdate, session: SessionDep) -> PurchaseOut:
    purchase = session.get(Purchase, purchase_id)
    if purchase is None:
        raise HTTPException(status_code=404, detail="purchase_not_found")
    data = body.model_dump(exclude_unset=True)
    if data.get("name"):
        purchase.name = data["name"].strip()
    if "category" in data:
        purchase.category = data["category"]
    if data.get("unit_price") is not None:
        purchase.unit_price_cents = data["unit_price"]
    if data.get("quantity") is not None:
        purchase.quantity = data["quantity"]
    purchase.total_cents = line_total_cents(purchase.unit_price_cents, purchase.quantity)
    session.add(purchase)
    session.commit()
    session.refresh(purchase)
    return PurchaseOut.model_validate(purchase)


@router.delete("/purchases/{purchase_id}", status_code=204)
def delete_purchase(purchase_id: int, session: SessionDep) -> None:
    purchase = session.get(Purchase, purchase_id)
    if purchase is None:
        raise HTTPException(status_code=404, detail="purchase_not_found")
    session.delete(purchase)
    session.commit()
