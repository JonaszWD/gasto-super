from fastapi import APIRouter, HTTPException, Query
from sqlmodel import Session, col, select, update

from app.categories import CATEGORIES
from app.deps import OffClientDep, RulesDep, SessionDep, last_unit_price, rules_key_for
from app.models import CanonicalProduct, Listing, Product, Purchase, Store, utcnow
from app.schemas import ProductOut, ProductUpsert, ScanResult
from app.services.barcode_parser import BarcodeKind, parse_barcode
from app.services.catalog import valid_ean
from app.services.openfoodfacts import OffLookupError
from app.services.synonyms import query_groups, relevance, text_matches

router = APIRouter(prefix="/api", tags=["products"])


def product_from_catalog(session: Session, product_key: str) -> Product | None:
    """A product the price collectors already know (e.g. Hacendado items from Mercadona's catalog)."""
    ean = valid_ean(product_key)
    canonical = session.exec(select(CanonicalProduct).where(CanonicalProduct.ean == ean)).first() if ean else None
    if canonical is None:
        return None
    image = session.exec(
        select(Listing.image_url).where(Listing.canonical_product_id == canonical.id, col(Listing.image_url).is_not(None))
    ).first()
    product = Product(key=product_key, name=canonical.name, brand=canonical.brand, image_url=image, source="catalog")
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


VALID_LENGTHS = (8, 12, 13, 14)


@router.get("/categories")
def list_categories() -> list[str]:
    return CATEGORIES


@router.get("/scan/{code}")
async def scan(
    code: str,
    session: SessionDep,
    off: OffClientDep,
    rules: RulesDep,
    store_id: int | None = None,
) -> ScanResult:
    store = session.get(Store, store_id) if store_id is not None else None
    parsed = parse_barcode(code, rules, rules_key_for(store))
    if len(parsed.code) not in VALID_LENGTHS:
        raise HTTPException(status_code=422, detail="invalid_barcode")

    product = session.get(Product, parsed.product_key)
    source = "cache" if product else "none"
    lookup_error = False
    if product is None and parsed.kind == BarcodeKind.STANDARD and parsed.valid_checksum:
        product = product_from_catalog(session, parsed.product_key)
        source = "catalog" if product else source

    # In-store (variable-weight) codes are meaningless to Open Food Facts; never look them up.
    if product is None and parsed.kind == BarcodeKind.STANDARD and parsed.valid_checksum:
        try:
            found = await off.lookup(parsed.code)
        except OffLookupError:
            found = None
            lookup_error = True
        if found is not None:
            product = Product(
                key=parsed.product_key,
                name=found.name,
                brand=found.brand,
                category=found.category,
                image_url=found.image_url,
                source="off",
            )
            session.add(product)
            session.commit()
            session.refresh(product)
            source = "off"

    last_price = last_unit_price(session, parsed.product_key)
    suggested_price = last_price
    suggested_qty = 1.0
    if parsed.kind == BarcodeKind.VARIABLE_PRICE:
        suggested_price = parsed.price_cents
    elif parsed.kind == BarcodeKind.VARIABLE_WEIGHT and parsed.weight_grams:
        # Quantity in kg, price per kg from the last purchase.
        suggested_qty = parsed.weight_grams / 1000

    return ScanResult(
        barcode=parsed.code,
        product_key=parsed.product_key,
        kind=parsed.kind,
        valid_checksum=parsed.valid_checksum,
        found=product is not None,
        source=source,
        lookup_error=lookup_error,
        product=ProductOut.model_validate(product) if product else None,
        embedded_price_cents=parsed.price_cents,
        weight_grams=parsed.weight_grams,
        last_unit_price_cents=last_price,
        suggested_unit_price_cents=suggested_price,
        suggested_quantity=suggested_qty,
    )


@router.get("/products")
def search_products(session: SessionDep, q: str = Query(default="", max_length=100)) -> list[ProductOut]:
    stmt = select(Product).order_by(Product.name)
    groups = query_groups(q)
    if not groups:
        return [ProductOut.model_validate(p) for p in session.exec(stmt.limit(100)).all()]
    # Personal-scale table: accent-insensitive, English-aware filtering and ranking in Python.
    found = [p for p in session.exec(stmt.limit(5000)).all() if text_matches(f"{p.name} {p.brand or ''}", groups)]
    found.sort(key=lambda p: (-relevance(p.name, p.brand, groups), len(p.name), p.name))
    return [ProductOut.model_validate(p) for p in found[:100]]


@router.get("/products/{key:path}")
def get_product(key: str, session: SessionDep) -> ProductOut:
    product = session.get(Product, key)
    if product is None:
        raise HTTPException(status_code=404, detail="product_not_found")
    return ProductOut.model_validate(product)


@router.put("/products/{key:path}")
def upsert_product(key: str, body: ProductUpsert, session: SessionDep) -> ProductOut:
    """Create (manual fallback) or rename a product; existing purchases follow the new name."""
    product = session.get(Product, key)
    if product is None:
        product = Product(key=key, name=body.name.strip(), category=body.category, brand=body.brand, source="manual")
    else:
        product.name = body.name.strip()
        product.category = body.category
        if body.brand is not None:
            product.brand = body.brand
        product.updated_at = utcnow()
    session.add(product)
    session.exec(
        update(Purchase)
        .where(col(Purchase.product_key) == key)
        .values(name=product.name, category=product.category)
    )
    session.commit()
    session.refresh(product)
    return ProductOut.model_validate(product)
