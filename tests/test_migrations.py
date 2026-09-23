"""The Alembic migrations must produce exactly what the models describe."""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine
from sqlmodel import SQLModel

from app import models  # noqa: F401
from app.tools.db_report import collect, render


def test_models_match_migrations(engine: Engine) -> None:
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), SQLModel.metadata)
    assert diff == []


def test_db_report(clean_db: Engine) -> None:
    db_bytes, tables = collect(clean_db)
    assert db_bytes > 0
    by_name = {t.name: t for t in tables}
    assert by_name["chain"].rows == 10 and by_name["store"].rows == 10
    md = render(db_bytes, tables, markdown=True)
    assert "| listingprice |" in md and "Neon" in md
