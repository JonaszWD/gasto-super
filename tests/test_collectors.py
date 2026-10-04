"""Collector runner and CLI, end to end against the test database (sources mocked)."""

import httpx
import pytest
from sqlalchemy import Engine
from sqlmodel import Session, select

from app.models import CollectorRun, Listing, ListingPrice
from app.sources.base import PoliteClient
from collectors import __main__ as cli
from collectors.runner import RunStats, run_source
from tests.sources_helpers import fixture, no_sleep
from tests.test_sources import alcampo_handler, consum_handler, easycompra_handler, lidl_handler, mercadona_handler


def client_for(handler) -> PoliteClient:  # type: ignore[no-untyped-def]
    return PoliteClient(transport=httpx.MockTransport(handler), min_interval=0, sleep=no_sleep, retries=0)


async def test_mercadona_run_stores_listings_prices_and_backfills_eans(clean_db: Engine) -> None:
    stats = await run_source(clean_db, "mercadona", http=client_for(mercadona_handler), max_detail=5)
    assert stats.status == "ok"
    assert stats.products_checked == 3 and stats.new_listings == 3 and stats.prices_changed == 3
    assert stats.eans_added == 1  # only product 4241 has a recorded detail response
    with Session(clean_db) as s:
        listings = s.exec(select(Listing)).all()
        assert {li.postal_code for li in listings} == {"28020"}
        assert next(li for li in listings if li.chain_product_id == "4241").ean == "8402001027482"
        run = s.exec(select(CollectorRun)).one()
        assert run.status == "ok" and run.products_checked == 3 and run.finished_at is not None
        # Warehouse lookup cached for the next night.
        from app.models import AppSetting

        assert s.get(AppSetting, "mercadona:wh:28020").value == "mad3"  # type: ignore[union-attr]

    again = await run_source(clean_db, "mercadona", http=client_for(mercadona_handler), max_detail=5)
    assert again.prices_changed == 0 and again.new_listings == 0  # unchanged prices -> no new rows
    with Session(clean_db) as s:
        assert len(s.exec(select(ListingPrice)).all()) == 3


async def test_bad_item_does_not_stop_the_run(clean_db: Engine) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/categories/112/":
            data = fixture("mercadona/category_112.json")
            data["categories"][0]["products"][1]["display_name"] = "x" * 500  # over-long name: trimmed, not an error
            data["categories"][0]["products"][2]["price_instructions"]["unit_price"] = "gratis"  # unparseable
            return httpx.Response(200, json=data)
        return mercadona_handler(request)

    stats = await run_source(clean_db, "mercadona", http=client_for(handler), max_detail=0)
    assert stats.products_checked == 2  # the unparseable one is skipped by the adapter
    assert stats.status == "ok"


async def test_source_failure_is_reported_not_raised(clean_db: Engine) -> None:
    stats = await run_source(clean_db, "mercadona", http=client_for(lambda r: httpx.Response(403)), max_detail=0)
    assert stats.status == "failed" and stats.errors == 1
    with Session(clean_db) as s:
        assert s.exec(select(CollectorRun)).one().status == "failed"


async def test_alcampo_run_stores_listings(clean_db: Engine) -> None:
    stats = await run_source(clean_db, "alcampo", http=client_for(alcampo_handler), limit=10)
    assert stats.status == "ok" and stats.products_checked == 4  # only "leche" has a recorded page
    with Session(clean_db) as s:
        assert {li.postal_code for li in s.exec(select(Listing)).all()} == {""}  # not location-specific


async def test_alcampo_blocked_run_fails_with_a_clear_message(clean_db: Engine) -> None:
    stats = await run_source(clean_db, "alcampo", http=client_for(lambda r: httpx.Response(403, text="")))
    assert stats.status == "failed"
    assert any("blocking requests" in m for m in stats.messages)
    with Session(clean_db) as s:
        assert s.exec(select(CollectorRun)).one().status == "failed"


async def test_consum_run_stores_listings_with_eans(clean_db: Engine) -> None:
    stats = await run_source(clean_db, "consum", http=client_for(consum_handler))
    assert stats.status == "ok" and stats.products_checked == 10 and stats.new_listings == 10
    with Session(clean_db) as s:
        listings = s.exec(select(Listing)).all()
        assert {li.postal_code for li in listings} == {""}  # not location-specific
        assert next(li for li in listings if li.chain_product_id == "1669").ean == "8423230065137"


async def test_lidl_run_stores_the_lidl_plus_label(clean_db: Engine) -> None:
    stats = await run_source(clean_db, "lidl", http=client_for(lidl_handler))
    assert stats.status == "ok" and stats.products_checked == 3
    with Session(clean_db) as s:
        listings = {li.chain_product_id: li for li in s.exec(select(Listing)).all()}
        assert {li.postal_code for li in listings.values()} == {""}  # one national price
        assert listings["11040383"].price_label == "lidl_plus"
        assert listings["11007752"].price_label is None


async def test_easycompra_stale_chain_is_partial(clean_db: Engine) -> None:
    stats = await run_source(clean_db, "easycompra", chains={"carrefour", "dia"}, http=client_for(easycompra_handler))
    assert stats.status == "partial" and stats.products_checked == 3
    assert any("dia" in m for m in stats.messages)
    with Session(clean_db) as s:
        assert {li.postal_code for li in s.exec(select(Listing)).all()} == {""}  # not location-specific


def test_cli_isolates_sources_and_writes_summary(
    clean_db: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path  # type: ignore[no-untyped-def]
) -> None:
    calls: list[str] = []

    async def fake_run(engine, source_id, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(source_id)
        if source_id == "easycompra":
            raise RuntimeError("boom")
        return RunStats(source_id, ["mercadona"], "28020", status="ok", products_checked=10, prices_changed=2)

    monkeypatch.setattr(cli, "run_source", fake_run)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    code = cli.main(["run", "--all"])
    assert calls == ["mercadona", "easycompra", "openprices", "alcampo", "consum", "lidl"]  # a crash doesn't stop the others
    assert code == 1  # but the job is marked failed
    text = summary.read_text()
    assert "| mercadona |" in text and "| failed |" in text


def test_cli_chain_without_source(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["run", "--chain", "aldi"]) == 0
    assert "No enabled source" in capsys.readouterr().out


def test_cli_list(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "mercadona" in out and "easycompra" in out and "alcampo" in out and "consum" in out
