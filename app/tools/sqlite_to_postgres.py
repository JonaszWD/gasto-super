"""One-time copy of the old SQLite database (spending tracker v1) into Postgres.

    DATABASE_URL=postgresql+psycopg://... uv run python -m app.tools.sqlite_to_postgres path/to/spending.db

Run `alembic upgrade head` first. Copies stores, products, trips and purchases. Stores are matched
by name (case-insensitive) to the seeded ones; trip ids are renumbered. Old timestamps were naive
UTC and are stored as UTC. Refuses to run if Postgres already has trips, unless --force.
To get the file out of the old Docker volume:
    docker run --rm -v spendingtracker_app-data:/data -v "$PWD":/out alpine cp /data/spending.db /out/
"""

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, func
from sqlmodel import Session, select

from app.config import get_settings
from app.db import make_engine
from app.models import Chain, Product, Purchase, Store, Trip
from app.services.barcode_parser import store_rules_key


@dataclass
class ImportStats:
    stores_created: int = 0
    products: int = 0
    trips: int = 0
    purchases: int = 0


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def copy_sqlite(sqlite_path: Path, engine: Engine, *, force: bool = False) -> ImportStats:
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    stats = ImportStats()
    with Session(engine) as s:
        if not force and s.exec(select(func.count()).select_from(Trip)).one() > 0:
            raise SystemExit("Postgres already has trips; use --force to import anyway.")
        chains = {c.id for c in s.exec(select(Chain)).all()}
        by_name = {st.name.lower(): st for st in s.exec(select(Store)).all()}
        store_map: dict[int, int] = {}
        for row in src.execute("SELECT id, name, rules_key FROM store"):
            store = by_name.get(row["name"].lower())
            if store is None:
                slug = store_rules_key(row["name"])
                store = Store(name=row["name"], rules_key=row["rules_key"], chain_id=slug if slug in chains else None)
                s.add(store)
                s.flush()
                by_name[row["name"].lower()] = store
                stats.stores_created += 1
            store_map[row["id"]] = store.id  # type: ignore[assignment]

        for row in src.execute("SELECT * FROM product"):
            key = row["key"] if "key" in row.keys() else row["barcode"]
            product = s.get(Product, key) or Product(key=key, name=row["name"])
            product.name, product.brand, product.category = row["name"], row["brand"], row["category"]
            product.image_url, product.source = row["image_url"], row["source"]
            product.created_at = _dt(row["created_at"]) or product.created_at
            product.updated_at = _dt(row["updated_at"]) or product.updated_at
            s.add(product)
            stats.products += 1

        trip_map: dict[int, int] = {}
        for row in src.execute("SELECT * FROM trip ORDER BY id"):
            trip = Trip(store_id=store_map[row["store_id"]], started_at=_dt(row["started_at"]), closed_at=_dt(row["closed_at"]))
            s.add(trip)
            s.flush()
            trip_map[row["id"]] = trip.id  # type: ignore[assignment]
            stats.trips += 1

        for row in src.execute("SELECT * FROM purchase ORDER BY id"):
            s.add(
                Purchase(
                    trip_id=trip_map[row["trip_id"]],
                    barcode=row["barcode"],
                    product_key=row["product_key"],
                    name=row["name"],
                    category=row["category"],
                    unit_price_cents=row["unit_price_cents"],
                    quantity=float(row["quantity"]),
                    total_cents=row["total_cents"],
                    weight_grams=row["weight_grams"],
                    created_at=_dt(row["created_at"]),
                )
            )
            stats.purchases += 1
        s.commit()
    src.close()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sqlite_path", type=Path)
    parser.add_argument("--force", action="store_true", help="import even if Postgres already has trips")
    args = parser.parse_args()
    if not args.sqlite_path.is_file():
        sys.exit(f"{args.sqlite_path} not found")
    stats = copy_sqlite(args.sqlite_path, make_engine(get_settings().database_url), force=args.force)
    print(
        f"Imported {stats.trips} trips, {stats.purchases} purchases, {stats.products} products "
        f"({stats.stores_created} new stores)."
    )


if __name__ == "__main__":
    main()
