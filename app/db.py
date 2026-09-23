"""Database engine. Plain Postgres via psycopg 3, configured only through DATABASE_URL."""

from collections.abc import Iterator
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.pool import NullPool
from sqlmodel import Session, create_engine

from app.config import get_settings

_engine: Engine | None = None


def normalize_url(url: str) -> str:
    """Accept the URLs Neon/Heroku-style providers hand out and select the psycopg 3 driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def make_engine(url: str, **kwargs: Any) -> Engine:
    # Serverless: every invocation may be a fresh process and Neon's PgBouncer does the pooling,
    # so don't keep connections around. prepare_threshold=None avoids server-side prepared
    # statements, which transaction-mode poolers can't route reliably.
    kwargs.setdefault("poolclass", NullPool)
    return create_engine(
        normalize_url(url),
        connect_args={"prepare_threshold": None, "connect_timeout": 5},
        pool_pre_ping=kwargs.pop("pool_pre_ping", False),
        **kwargs,
    )


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = make_engine(get_settings().database_url)
    return _engine


def set_engine(engine: Engine | None) -> None:
    """Override the engine (used by tests)."""
    global _engine
    _engine = engine


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
