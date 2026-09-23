import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlmodel import Session, select

from app.models import Product, Purchase, Store, Trip
from app.tools.sqlite_to_postgres import copy_sqlite

# Schema of the v1 (SQLite) app.
V1_SCHEMA = """
CREATE TABLE store (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL UNIQUE, rules_key VARCHAR);
CREATE TABLE product (key VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, brand VARCHAR, category VARCHAR,
  image_url VARCHAR, source VARCHAR NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL);
CREATE TABLE trip (id INTEGER PRIMARY KEY, store_id INTEGER NOT NULL, started_at DATETIME NOT NULL, closed_at DATETIME);
CREATE TABLE purchase (id INTEGER PRIMARY KEY, trip_id INTEGER NOT NULL, barcode VARCHAR NOT NULL,
  product_key VARCHAR NOT NULL, name VARCHAR NOT NULL, category VARCHAR, unit_price_cents INTEGER NOT NULL,
  quantity FLOAT NOT NULL, total_cents INTEGER NOT NULL, weight_grams INTEGER, created_at DATETIME NOT NULL);
"""


@pytest.fixture
def v1_db(tmp_path: Path) -> Path:
    path = tmp_path / "spending.db"
    con = sqlite3.connect(path)
    con.executescript(V1_SCHEMA)
    con.executescript("""
      INSERT INTO store VALUES (1, 'Mercadona', NULL), (11, 'Bonpreu', NULL);
      INSERT INTO product VALUES ('8480000123459', 'Leche entera', NULL, 'Lácteos y huevos', NULL, 'manual',
        '2026-09-01 10:00:00.000000', '2026-09-01 10:00:00.000000');
      INSERT INTO trip VALUES (7, 1, '2026-09-01 09:55:00.000000', '2026-09-01 10:30:00.000000'),
                              (8, 11, '2026-09-02 18:00:00.000000', NULL);
      INSERT INTO purchase VALUES (1, 7, '8480000123459', '8480000123459', 'Leche entera', 'Lácteos y huevos',
        95, 6.0, 570, NULL, '2026-09-01 10:00:00.000000'),
        (2, 8, '2112345003495', 'vw:21:12345', 'Pollo', NULL, 349, 1.0, 349, NULL, '2026-09-02 18:05:00.000000');
    """)
    con.commit()
    con.close()
    return path


def test_copy_sqlite(clean_db: Engine, v1_db: Path) -> None:
    stats = copy_sqlite(v1_db, clean_db)
    assert (stats.trips, stats.purchases, stats.products, stats.stores_created) == (2, 2, 1, 1)
    with Session(clean_db) as s:
        trips = s.exec(select(Trip).order_by(Trip.id)).all()  # type: ignore[arg-type]
        stores = {st.id: st.name for st in s.exec(select(Store)).all()}
        assert [stores[t.store_id] for t in trips] == ["Mercadona", "Bonpreu"]
        assert trips[0].started_at.tzinfo is not None and trips[0].started_at.hour == 9  # UTC kept
        assert trips[1].closed_at is None
        purchases = s.exec(select(Purchase).order_by(Purchase.id)).all()  # type: ignore[arg-type]
        assert [(p.total_cents, p.quantity) for p in purchases] == [(570, 6.0), (349, 1.0)]
        assert s.get(Product, "8480000123459").name == "Leche entera"  # type: ignore[union-attr]

    with pytest.raises(SystemExit):
        copy_sqlite(v1_db, clean_db)  # refuses a second import
