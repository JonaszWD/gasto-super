from fastapi import APIRouter, HTTPException
from sqlmodel import func, select

from app.deps import SessionDep
from app.models import Chain, Store
from app.schemas import StoreCreate, StoreOut
from app.services.barcode_parser import store_rules_key

router = APIRouter(prefix="/api/stores", tags=["stores"])


@router.get("")
def list_stores(session: SessionDep) -> list[StoreOut]:
    return [StoreOut.model_validate(s) for s in session.exec(select(Store).order_by(Store.id)).all()]


@router.post("", status_code=201)
def create_store(body: StoreCreate, session: SessionDep) -> StoreOut:
    name = " ".join(body.name.split())
    exists = session.exec(select(Store).where(func.lower(Store.name) == name.lower())).first()
    if exists:
        raise HTTPException(status_code=409, detail="store_exists")
    # "Dia Market" -> chain "dia", so paid prices appear in the price comparison.
    slug = store_rules_key(name)
    chain = next((c for c in session.exec(select(Chain)).all() if slug == c.id or slug.startswith(c.id + "-")), None)
    store = Store(name=name, chain_id=chain.id if chain else None)
    session.add(store)
    session.commit()
    session.refresh(store)
    return StoreOut.model_validate(store)
