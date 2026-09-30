import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.sources.alcampo import AlcampoAdapter
from app.sources.base import SourceAdapter
from app.sources.easycompra import EasyCompraAdapter
from app.sources.mercadona import MercadonaAdapter
from app.sources.openprices import OpenPricesAdapter

CONFIG_PATH = Path(__file__).with_name("sources.toml")


@lru_cache
def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def build_adapter(source_id: str, config: dict[str, Any] | None = None) -> SourceAdapter | None:
    cfg = (config or load_config()).get(source_id, {})
    adapter: SourceAdapter
    match source_id:
        case "mercadona":
            adapter = MercadonaAdapter(cfg.get("departments"), cfg.get("subcategories"))
        case "easycompra":
            adapter = EasyCompraAdapter()
            if cfg.get("chains"):
                adapter.chains = tuple(cfg["chains"])
        case "openprices":
            adapter = OpenPricesAdapter(
                radius_km=cfg.get("radius_km", 10), days=cfg.get("days", 120), max_pages=cfg.get("max_pages", 20)
            )
        case "alcampo":
            adapter = AlcampoAdapter(cfg.get("departments"), cfg.get("search_terms"), cfg.get("max_products_per_run"))
        case _:
            return None
    adapter.min_interval = cfg.get("min_interval_seconds", adapter.min_interval)
    return adapter


ALL_SOURCES = ("mercadona", "easycompra", "openprices", "alcampo")


def enabled_sources(config: dict[str, Any] | None = None) -> list[str]:
    cfg = config or load_config()
    return [s for s in ALL_SOURCES if cfg.get(s, {}).get("enabled", False)]


def sources_for_chain(chain_id: str, config: dict[str, Any] | None = None) -> list[str]:
    """Chain-specific sources for `chain_id` (Open Prices is multi-chain and runs on its own)."""
    out = []
    for source_id in enabled_sources(config):
        if source_id == "openprices":
            continue
        adapter = build_adapter(source_id, config)
        if adapter and chain_id in adapter.chains:
            out.append(source_id)
    return out
