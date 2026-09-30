from fastapi import APIRouter, HTTPException
from sqlmodel import Session, col, select

from app.deps import SessionDep, SettingsDep
from app.models import CanonicalProduct, ShoppingListItem
from app.schemas import (
    ChainTotalOut,
    ListItemIn,
    ListLineOut,
    ListTypeAmountIn,
    ListTypeIn,
    ShoppingListOut,
    SplitOut,
)
from app.services.compare import ListEntry, chain_names, compare_list, postal_code
from app.services.names import clean_name
from app.services.product_types import load_types

router = APIRouter(prefix="/api/shopping-list", tags=["shopping-list"])


def _line_out(line) -> ListLineOut:  # type: ignore[no-untyped-def]
    pt = load_types().get(line.product_type) if line.product_type else None
    return ListLineOut(**{**line.__dict__, "name_en": pt.en if pt else None})


def _render(session: Session, settings) -> ShoppingListOut:  # type: ignore[no-untyped-def]
    rows = session.exec(
        select(ShoppingListItem, CanonicalProduct)
        .join(CanonicalProduct, col(CanonicalProduct.id) == ShoppingListItem.canonical_product_id, isouter=True)
        .order_by(col(ShoppingListItem.created_at), col(ShoppingListItem.id))
    ).all()
    if not rows:
        return ShoppingListOut(items=[], chains=[], split=None)
    types = load_types()
    entries: list[ListEntry] = []
    items: list[ListLineOut] = []
    for item, product in rows:
        if product is not None:
            entries.append(ListEntry(f"p:{product.id}", clean_name(product.name, product.brand), product, item.quantity))
            items.append(ListLineOut(key=f"p:{product.id}", product_id=product.id, name=product.name,
                                     quantity=item.quantity, price_cents=None, match=""))
        else:
            slug = item.product_type or ""
            pt = types.get(slug)  # a type removed from product_types.toml stays, priced nowhere
            entries.append(ListEntry(f"t:{slug}", pt.es if pt else slug, product_type=slug,
                                     amount=item.amount, unit=item.amount_unit))
            items.append(ListLineOut(key=f"t:{slug}", product_id=None, name=pt.es if pt else slug,
                                     name_en=pt.en if pt else None, quantity=1, price_cents=None, match="",
                                     product_type=slug, amount=item.amount, unit=item.amount_unit))
    pc = postal_code(session, settings.default_postal_code)
    totals, split = compare_list(session, entries, pc, settings.stale_after_days)
    names = chain_names(session)
    return ShoppingListOut(
        items=items,
        chains=[ChainTotalOut(**{**t.__dict__, "lines": [_line_out(ln) for ln in t.lines]}) for t in totals],
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


# A product type on the list ("Pechuga de pollo, 1 kg"): each chain is priced with its cheapest
# way to buy that amount (services/compare.cheapest_for_amount).


def _type_item(session: Session, slug: str) -> ShoppingListItem | None:
    return session.exec(select(ShoppingListItem).where(ShoppingListItem.product_type == slug)).first()


@router.post("/types", status_code=201)
def add_type(body: ListTypeIn, session: SessionDep, settings: SettingsDep) -> ShoppingListOut:
    if body.product_type not in load_types():
        raise HTTPException(status_code=404, detail="type_not_found")
    item = _type_item(session, body.product_type)
    if item and item.amount_unit == body.unit:
        item.amount = min(100.0, (item.amount or 0) + body.amount)
    elif item:  # the type's unit changed since: start over in the new one
        item.amount, item.amount_unit = body.amount, body.unit
    else:
        item = ShoppingListItem(product_type=body.product_type, amount=body.amount, amount_unit=body.unit)
    session.add(item)
    session.commit()
    return _render(session, settings)


@router.put("/types/{slug}")
def set_type_amount(slug: str, body: ListTypeAmountIn, session: SessionDep, settings: SettingsDep) -> ShoppingListOut:
    item = _type_item(session, slug)
    if item is None:
        raise HTTPException(status_code=404, detail="item_not_found")
    item.amount = body.amount
    session.add(item)
    session.commit()
    return _render(session, settings)


@router.delete("/types/{slug}")
def remove_type(slug: str, session: SessionDep, settings: SettingsDep) -> ShoppingListOut:
    item = _type_item(session, slug)
    if item:
        session.delete(item)
        session.commit()
    return _render(session, settings)
