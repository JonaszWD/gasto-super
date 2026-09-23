from fastapi import APIRouter, HTTPException
from sqlmodel import Session, col, select

from app.deps import SessionDep, SettingsDep
from app.models import CanonicalProduct, ShoppingListItem
from app.schemas import ChainTotalOut, ListItemIn, ListLineOut, ShoppingListOut, SplitOut
from app.services.compare import chain_names, compare_list, postal_code

router = APIRouter(prefix="/api/shopping-list", tags=["shopping-list"])


def _render(session: Session, settings) -> ShoppingListOut:  # type: ignore[no-untyped-def]
    rows = session.exec(
        select(ShoppingListItem, CanonicalProduct)
        .join(CanonicalProduct, col(CanonicalProduct.id) == ShoppingListItem.canonical_product_id)
        .order_by(col(ShoppingListItem.created_at))
    ).all()
    items = [(product, item.quantity) for item, product in rows]
    lines = [ListLineOut(product_id=p.id, name=p.name, quantity=q, price_cents=None, match="") for p, q in items]  # type: ignore[arg-type]
    if not items:
        return ShoppingListOut(items=[], chains=[], split=None)
    pc = postal_code(session, settings.default_postal_code)
    totals, split = compare_list(session, items, pc, settings.stale_after_days)
    names = chain_names(session)
    return ShoppingListOut(
        items=lines,
        chains=[ChainTotalOut(**{**t.__dict__, "lines": [ListLineOut(**ln.__dict__) for ln in t.lines]}) for t in totals],
        split=SplitOut(
            chain_ids=list(split.chain_ids),
            chain_names=[names.get(c, c) for c in split.chain_ids],
            total_cents=split.total_cents,
            missing=split.missing,
            assignment=split.assignment,
        )
        if split
        else None,
    )


@router.get("")
def get_list(session: SessionDep, settings: SettingsDep) -> ShoppingListOut:
    return _render(session, settings)


@router.post("/items", status_code=201)
def add_item(body: ListItemIn, session: SessionDep, settings: SettingsDep) -> ShoppingListOut:
    if session.get(CanonicalProduct, body.product_id) is None:
        raise HTTPException(status_code=404, detail="product_not_found")
    item = session.exec(select(ShoppingListItem).where(ShoppingListItem.canonical_product_id == body.product_id)).first()
    if item:
        item.quantity = min(99, item.quantity + body.quantity)
    else:
        item = ShoppingListItem(canonical_product_id=body.product_id, quantity=body.quantity)
    session.add(item)
    session.commit()
    return _render(session, settings)


@router.put("/items/{product_id}")
def set_quantity(product_id: int, body: ListItemIn, session: SessionDep, settings: SettingsDep) -> ShoppingListOut:
    item = session.exec(select(ShoppingListItem).where(ShoppingListItem.canonical_product_id == product_id)).first()
    if item is None:
        raise HTTPException(status_code=404, detail="item_not_found")
    item.quantity = body.quantity
    session.add(item)
    session.commit()
    return _render(session, settings)


@router.delete("/items/{product_id}")
def remove_item(product_id: int, session: SessionDep, settings: SettingsDep) -> ShoppingListOut:
    item = session.exec(select(ShoppingListItem).where(ShoppingListItem.canonical_product_id == product_id)).first()
    if item:
        session.delete(item)
        session.commit()
    return _render(session, settings)
