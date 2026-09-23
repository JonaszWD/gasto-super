"""Test setup: a real Postgres database (TEST_DATABASE_URL), migrated once with Alembic.

Locally: `docker compose up -d db` (creates the gasto_test database). CI uses a service container.
"""

import importlib.util
import os
from collections.abc import Iterator
from pathlib import Path

# Must be set before app modules read settings.
os.environ.setdefault("TEST_DATABASE_URL", "postgresql+psycopg://gasto:gasto@localhost:5433/gasto_test")
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ["SESSION_SECRET"] = "test-secret-" + "x" * 40
os.environ["COOKIE_SECURE"] = "false"  # TestClient talks plain http
TEST_PASSWORD = "correct horse battery"

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import Engine, text  # noqa: E402

from app.auth import hash_password  # noqa: E402

os.environ["APP_PASSWORD_HASH"] = hash_password(TEST_PASSWORD)

from app import db  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.deps import get_off_client  # noqa: E402
from app.services.openfoodfacts import OffLookupError, OffProduct  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
get_settings.cache_clear()


def _migration_seed() -> list[tuple[str, str]]:
    spec = importlib.util.spec_from_file_location("m0001", ROOT / "migrations/versions/0001_initial_schema.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.CHAINS


class FakeOff:
    def __init__(self) -> None:
        self.products: dict[str, OffProduct] = {}
        self.calls: list[str] = []
        self.fail = False

    async def lookup(self, barcode: str) -> OffProduct | None:
        self.calls.append(barcode)
        if self.fail:
            raise OffLookupError("offline")
        return self.products.get(barcode)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = db.make_engine(os.environ["TEST_DATABASE_URL"])
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    cfg = Config(str(ROOT / "alembic.ini"))
    with eng.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    db.set_engine(eng)
    yield eng
    db.set_engine(None)
    eng.dispose()


@pytest.fixture
def clean_db(engine: Engine) -> Engine:
    """Empty every table and restore the migration's seed data."""
    with engine.begin() as conn:
        tables = [
            r[0]
            for r in conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'")
            )
        ]
        conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
        for cid, name in _migration_seed():
            conn.execute(text("INSERT INTO chain (id, name) VALUES (:i, :n)"), {"i": cid, "n": name})
            conn.execute(text("INSERT INTO store (name, chain_id) VALUES (:n, :i)"), {"i": cid, "n": name})
        conn.execute(text("INSERT INTO appsetting (key, value) VALUES ('postal_code', '28020')"))
    return engine


@pytest.fixture
def fake_off() -> FakeOff:
    return FakeOff()


@pytest.fixture
def anon_client(clean_db: Engine, fake_off: FakeOff) -> Iterator[TestClient]:
    from app.main import app

    app.dependency_overrides[get_off_client] = lambda: fake_off
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def client(anon_client: TestClient) -> TestClient:
    r = anon_client.post("/api/auth/login", json={"password": TEST_PASSWORD})
    assert r.status_code == 204, r.text
    return anon_client


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Run every `async def` test with anyio (no per-test marker needed)."""
    import inspect

    for item in items:
        if isinstance(item, pytest.Function) and inspect.iscoroutinefunction(item.function):
            item.add_marker(pytest.mark.anyio)
