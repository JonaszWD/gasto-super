"""Alembic environment. The database comes only from DATABASE_URL (never hard-coded).

Run with `uv run alembic upgrade head` (locally, or from the manual GitHub Actions workflow).
Migrations are never run at app startup.
"""

from logging.config import fileConfig

from alembic import context
from sqlmodel import SQLModel

from app import models  # noqa: F401  (registers tables on SQLModel.metadata)
from app.config import get_settings
from app.db import make_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")
    if connectable is not None:  # tests pass an existing connection
        _run(connectable)
        return
    engine = make_engine(get_settings().database_url)
    with engine.connect() as connection:
        _run(connection)
    engine.dispose()


def _run(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
