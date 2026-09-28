"""Catalogue export import (collectors/xlsx_import.py), against the test database."""

from pathlib import Path

import pytest
from openpyxl import Workbook
from sqlalchemy import Engine
from sqlmodel import Session, select

from app.models import CanonicalProduct, CollectorRun, Listing, ListingPrice
from app.services.catalog import RawListing, record_listing_price, upsert_listing
from app.services.quantity import Quantity
from collectors import __main__ as cli
from collectors.xlsx_import import department_for, import_listings, parse_files, resolve_quantity

HEADER = ["Id", "Nombre", "Precio", "Precio Pack", "Formato", "Categoria", "Supermercado", "Url", "Url_imagen"]
CARREFOUR_ROWS = [
    ["VC4AECOMM-485696", "Yogur griego Carrefour 1 kg.", "1,79 €", "1,79 €", "kg",
     "/supermercado/la-despensa-promocion/F-13ji8Z13rjo/c", "Carrefour", "https://c/1", "https://c/1.jpg"],
    # Same product under a regular category: this row wins over the promo one.
    ["VC4AECOMM-485696", "Yogur griego Carrefour 1 kg.", "1,79 €", "1,79 €", "kg",
     "/supermercado/frescos/yogures/cat1/c", "Carrefour", "https://c/1", "https://c/1.jpg"],
    ["521", "Crema de manos Neutrogena 75 ml.", "3,20 €", "4,27 €", "100ml",
     "/supermercado/parafarmacia/cuidado-de-manos-y-pies/cat2/c", "Carrefour", "https://c/2", "https://c/2.jpg"],
    ["522", "Champú Johnson's 500 ml.", "2.463,33 €", "0,50 €", "100ml",
     "/supermercado/la-despensa/alimentacion/cat3/c", "Carrefour", "https://c/3", "https://c/3.jpg"],
]
DIA_ROWS = [
    ["108589", "Queso curado Dia El Cencerro 250 g", 2.85, 11.4, "KILO", "/quesos/curado/c/L2007", "Dia",
     "https://d/1", "https://d/1.jpg"],
    ["272536", "Agua mineral Dia", 0.9, 0.6, "LITRO", "/agua-y-refrescos/agua/c/L1", "Dia", "https://d/2", "https://d/2.jpg"],
    ["300000", "Pañales talla 4", 9.99, 0.2, "UNIDAD", "/infantil/panales-y-toallitas/c/L3", "Dia", "https://d/3", None],
    ["4241", "Aceite de oliva Hacendado", 17.25, 3.45, "l", "112", "Mercadona", "https://m/1", "https://m/1.jpg"],
]


def write_xlsx(path: Path, rows: list[list]) -> Path:  # type: ignore[type-arg]
    wb = Workbook()
    ws = wb.active
    ws.append(HEADER)
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


@pytest.fixture
def files(tmp_path: Path) -> list[Path]:
    return [
        write_xlsx(tmp_path / "products_carrefour_24092026-122653.xlsx", CARREFOUR_ROWS),
        write_xlsx(tmp_path / "products_dia_24092026-122757.xlsx", DIA_ROWS),
    ]


def test_parse_dedupes_skips_and_normalises(files: list[Path]) -> None:
    listings, stats = parse_files(files)
    by_id = {li.chain_product_id: li for li in listings}
    assert set(by_id) == {"VC4AECOMM-485696", "522", "108589", "272536"}
    assert stats.duplicates == 1 and stats.skipped_category == 2 and stats.skipped_chain["mercadona"] == 1

    yogur = by_id["VC4AECOMM-485696"]
    assert yogur.category == "supermercado/frescos/yogures/cat1/c"  # regular category preferred over promo
    assert (yogur.price_cents, yogur.unit_price_cents, yogur.quantity) == (179, 179, Quantity(1.0, "kg"))
    assert yogur.observed_at is not None and yogur.observed_at.isoformat() == "2026-09-24T12:26:53+02:00"

    assert by_id["522"].price_cents == 246333  # thousands separator
    assert by_id["108589"].unit_price_cents == 1140
    agua = by_id["272536"]  # no size in the name: derived from price / reference price
    assert agua.department == "drink" and agua.quantity == Quantity(1.5, "l") and agua.unit_price_cents == 60


def test_resolve_quantity_units() -> None:
    # Per 100 ml reference -> per litre.
    assert resolve_quantity("Gel 750 ml", 300, 40, "100 ML.") == (Quantity(0.75, "l"), 400)
    # Reference in another unit than the name's size: unit price computed from the size.
    assert resolve_quantity("Mascarilla 1 ud.", 500, 300, "100g") == (Quantity(1.0, "unit"), None)
    # Implausible reference (off by more than 2x): ignored.
    assert resolve_quantity("Arroz 1 kg", 150, 900, "kg") == (Quantity(1.0, "kg"), None)
    assert resolve_quantity("Papel aluminio 30 metros", 199, 7, "m") == (None, None)


def test_department_rules() -> None:
    assert department_for("carrefour", "/supermercado/drogueria-y-limpieza/menaje/cat/c", "Sartén") is None
    assert department_for("carrefour", "/supermercado/drogueria-y-limpieza/limpieza-cocina/c", "Lejía") == "household"
    assert department_for("dia", "/cafe-cacao-e-infusiones/cafe-molido/c/L1", "Café molido") == "drink"
    assert department_for("dia", "/aceites-salsas-y-especias/c/L1", "Aceite de oliva") == "food"


def test_import_records_listings_and_is_idempotent(clean_db: Engine, files: list[Path]) -> None:
    listings, stats = parse_files(files)
    result = import_listings(clean_db, listings, stats)
    assert result.status == "ok" and result.products_checked == 4 and result.new_listings == 4
    with Session(clean_db) as s:
        rows = s.exec(select(Listing)).all()
        assert {li.source for li in rows} == {"xlsx"} and {li.postal_code for li in rows} == {""}
        assert len(s.exec(select(CanonicalProduct)).all()) == 4
        assert s.exec(select(CollectorRun)).one().status == "ok"

    again = import_listings(clean_db, *parse_files(files))
    assert again.new_listings == 0 and again.prices_changed == 0
    with Session(clean_db) as s:
        assert len(s.exec(select(ListingPrice)).all()) == 4


def test_older_export_does_not_overwrite_newer_prices(clean_db: Engine, tmp_path: Path) -> None:
    new = write_xlsx(tmp_path / "products_dia_25092026-100000.xlsx", DIA_ROWS[:1])
    old_rows = [DIA_ROWS[0][:2] + [2.50, 10.0] + DIA_ROWS[0][4:]]
    old = write_xlsx(tmp_path / "products_dia_20092026-100000.xlsx", old_rows)
    import_listings(clean_db, *parse_files([new]))
    assert import_listings(clean_db, *parse_files([old])).prices_changed == 0
    with Session(clean_db) as s:
        assert [p.price_cents for p in s.exec(select(ListingPrice)).all()] == [285]


def test_reuses_other_sources_ean_and_canonical(clean_db: Engine, files: list[Path]) -> None:
    with Session(clean_db) as s:
        with_ean = RawListing("carrefour", "VC4AECOMM-485696", "Yogur griego", 179, "food", ean="3560071246136")
        without = RawListing("dia", "108589", "Queso curado El Cencerro", 285, "food")
        for raw in (with_ean, without):
            listing, _ = upsert_listing(s, "easycompra", raw, "")
            record_listing_price(s, listing, raw)
        s.commit()

    result = import_listings(clean_db, *parse_files(files))
    assert result.eans_added == 1
    with Session(clean_db) as s:
        for chain, pid in (("carrefour", "VC4AECOMM-485696"), ("dia", "108589")):
            pair = s.exec(select(Listing).where(Listing.chain_id == chain, Listing.chain_product_id == pid)).all()
            assert len(pair) == 2 and len({li.canonical_product_id for li in pair}) == 1
        assert len(s.exec(select(CanonicalProduct)).all()) == 4  # 2 shared + 2 xlsx-only


def test_cli_dry_run(files: list[Path], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["import-xlsx", *map(str, files), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "to import: 4" in out and "skipped chain mercadona: 1 (use the live source)" in out


def test_cli_needs_a_date(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write_xlsx(tmp_path / "export.xlsx", DIA_ROWS[:1])
    assert cli.main(["import-xlsx", str(path), "--dry-run"]) == 2
    assert "--observed-at" in capsys.readouterr().err
    assert cli.main(["import-xlsx", str(path), "--dry-run", "--observed-at", "2026-09-24"]) == 0
