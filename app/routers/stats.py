import csv
import io
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from sqlmodel import Session, col, select

from app.categories import CATEGORIES_EN, UNCATEGORIZED
from app.deps import SessionDep, SettingsDep
from app.models import Purchase, Store, Trip
from app.schemas import Bucket, PriceHistory, PricePoint, ProductSummary, StatsOut

router = APIRouter(prefix="/api", tags=["stats"])

Range = Literal["month", "3months", "year", "all"]
N_WEEKS = 12
N_MONTHS = 12


def _rows(session: Session) -> list[tuple[Purchase, str]]:
    stmt = (
        select(Purchase, Store.name)
        .join(Trip, col(Trip.id) == Purchase.trip_id)
        .join(Store, col(Store.id) == Trip.store_id)
        .order_by(col(Purchase.created_at), col(Purchase.id))
    )
    return [(p, name) for p, name in session.exec(stmt).all()]


def _local(dt: datetime, tz: ZoneInfo) -> datetime:
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(tz)


def _month_start(d: date, months_back: int = 0) -> date:
    idx = d.year * 12 + d.month - 1 - months_back
    return date(idx // 12, idx % 12 + 1, 1)


def _range_start(today: date, rng: Range) -> date | None:
    match rng:
        case "month":
            return _month_start(today)
        case "3months":
            return _month_start(today, 2)
        case "year":
            return date(today.year, 1, 1)
        case "all":
            return None


def compute_stats(rows: list[tuple[Purchase, str]], tz: ZoneInfo, today: date, rng: Range) -> StatsOut:
    this_monday = today - timedelta(days=today.weekday())
    weeks = [this_monday - timedelta(weeks=i) for i in reversed(range(N_WEEKS))]
    months = [_month_start(today, i) for i in reversed(range(N_MONTHS))]
    weekly: dict[date, int] = dict.fromkeys(weeks, 0)
    monthly: dict[date, int] = dict.fromkeys(months, 0)
    by_cat: dict[str, int] = defaultdict(int)
    by_store: dict[str, int] = defaultdict(int)
    start = _range_start(today, rng)
    range_total = 0

    for purchase, store_name in rows:
        d = _local(purchase.created_at, tz).date()
        wk = d - timedelta(days=d.weekday())
        if wk in weekly:
            weekly[wk] += purchase.total_cents
        mo = d.replace(day=1)
        if mo in monthly:
            monthly[mo] += purchase.total_cents
        if start is None or d >= start:
            by_cat[purchase.category or UNCATEGORIZED] += purchase.total_cents
            by_store[store_name] += purchase.total_cents
            range_total += purchase.total_cents

    def ranked(values: dict[str, int]) -> list[Bucket]:
        return [Bucket(label=k, total_cents=v) for k, v in sorted(values.items(), key=lambda kv: -kv[1])]

    return StatsOut(
        weekly=[
            Bucket(label=f"{w.isocalendar().year}-W{w.isocalendar().week:02d}", start=w.isoformat(), total_cents=v)
            for w, v in weekly.items()
        ],
        monthly=[Bucket(label=m.strftime("%Y-%m"), start=m.isoformat(), total_cents=v) for m, v in monthly.items()],
        by_category=ranked(by_cat),
        by_store=ranked(by_store),
        range_total_cents=range_total,
    )


@router.get("/stats")
def stats(session: SessionDep, settings: SettingsDep, range_: Range = Query("month", alias="range")) -> StatsOut:
    tz = ZoneInfo(settings.timezone)
    return compute_stats(_rows(session), tz, datetime.now(tz).date(), range_)


@router.get("/stats/products")
def stats_products(session: SessionDep) -> list[ProductSummary]:
    latest: dict[str, Purchase] = {}
    counts: dict[str, int] = defaultdict(int)
    for purchase, _ in _rows(session):
        latest[purchase.product_key] = purchase
        counts[purchase.product_key] += 1
    summaries = [
        ProductSummary(product_key=k, name=p.name, purchases=counts[k], last_unit_price_cents=p.unit_price_cents)
        for k, p in latest.items()
    ]
    return sorted(summaries, key=lambda s: (-s.purchases, s.name.lower()))


@router.get("/stats/price-history/{product_key:path}")
def price_history(product_key: str, session: SessionDep) -> PriceHistory:
    points = [(p, s) for p, s in _rows(session) if p.product_key == product_key]
    if not points:
        raise HTTPException(status_code=404, detail="product_not_found")
    return PriceHistory(
        product_key=product_key,
        name=points[-1][0].name,
        points=[PricePoint(date=p.created_at, unit_price_cents=p.unit_price_cents, store_name=s) for p, s in points],
    )


def _es_number(value: float, decimals: int) -> str:
    return f"{value:.{decimals}f}".replace(".", ",")


CSV_HEADERS = {
    "es": ["fecha", "hora", "tienda", "compra_id", "codigo", "producto", "categoria", "precio_unitario", "cantidad", "total"],
    "en": ["date", "time", "store", "trip_id", "barcode", "product", "category", "unit_price", "quantity", "total"],
}


@router.get("/export.csv")
def export_csv(session: SessionDep, settings: SettingsDep, lang: Literal["es", "en"] = "es") -> Response:
    tz = ZoneInfo(settings.timezone)
    buf = io.StringIO()
    # Semicolons, decimal commas + BOM so Excel/Numbers in a Spanish locale open it correctly.
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(CSV_HEADERS[lang])
    for p, store_name in _rows(session):
        local = _local(p.created_at, tz)
        category = p.category or ""
        if lang == "en":
            category = CATEGORIES_EN.get(category, category)
        writer.writerow(
            [
                local.strftime("%d/%m/%Y"),
                local.strftime("%H:%M"),
                store_name,
                p.trip_id,
                p.barcode,
                p.name,
                category,
                _es_number(p.unit_price_cents / 100, 2),
                _es_number(p.quantity, 3).rstrip("0").rstrip(","),
                _es_number(p.total_cents / 100, 2),
            ]
        )
    prefix = "purchases" if lang == "en" else "compras"
    filename = f"{prefix}-{datetime.now(tz):%Y%m%d}.csv"
    return Response(
        content="\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
