"""Runs one price source end to end and records a CollectorRun row."""

import logging
from dataclasses import dataclass, field

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from app.config import get_settings
from app.models import CollectorRun, Listing, utcnow
from app.services.catalog import record_listing_price, upsert_listing
from app.services.compare import postal_code as current_postal_code
from app.services.kv import DbCache
from app.sources.base import PoliteClient, SourceContext
from app.sources.registry import build_adapter, load_config

log = logging.getLogger("collectors")
COMMIT_EVERY = 200


@dataclass
class RunStats:
    source: str
    chains: list[str]
    postal_code: str
    status: str = "running"
    products_checked: int = 0
    prices_changed: int = 0
    new_listings: int = 0
    eans_added: int = 0
    errors: int = 0
    messages: list[str] = field(default_factory=list)
    requests: int = 0


async def run_source(
    engine: Engine,
    source_id: str,
    *,
    chains: set[str] | None = None,
    postal_code: str | None = None,
    limit: int | None = None,
    max_detail: int | None = None,
    http: PoliteClient | None = None,
) -> RunStats:
    settings = get_settings()
    config = load_config()
    adapter = build_adapter(source_id, config)
    if adapter is None:
        raise ValueError(f"unknown source {source_id!r}")

    with Session(engine) as session:
        pc = postal_code or current_postal_code(session, settings.default_postal_code)
        listing_pc = pc if adapter.location_specific else ""
        stats = RunStats(source_id, sorted(chains or adapter.chains), pc)
        run = CollectorRun(source=source_id, postal_code=listing_pc)
        session.add(run)
        session.commit()

        client = http or PoliteClient(
            user_agent=adapter.user_agent or settings.sources_user_agent, min_interval=adapter.min_interval
        )
        ctx = SourceContext(postal_code=pc, http=client, cache=DbCache(session), chains=chains)
        try:
            await _collect_catalog(session, adapter, ctx, listing_pc, stats, limit)
            if adapter.supports_live_refresh:
                budget = max_detail if max_detail is not None else config.get(source_id, {}).get("max_detail_fetches", 0)
                await _backfill_eans(session, adapter, ctx, listing_pc, run, stats, budget)
        finally:
            stats.requests = client.request_count
            if http is None:
                await client.aclose()

        stats.messages.extend(ctx.warnings)
        if stats.status == "running":
            stats.status = "partial" if stats.errors or ctx.warnings else "ok"
        run.finished_at = utcnow()
        run.status = stats.status
        run.products_checked = stats.products_checked
        run.prices_changed = stats.prices_changed
        run.new_listings = stats.new_listings
        run.errors = stats.errors
        run.error_messages = "\n".join(stats.messages)[:4000]
        session.add(run)
        session.commit()
    return stats


async def _collect_catalog(session, adapter, ctx, listing_pc, stats: RunStats, limit) -> None:  # type: ignore[no-untyped-def]
    try:
        async for raw in adapter.fetch_catalog(ctx):
            if limit is not None and stats.products_checked >= limit:
                break
            try:
                with session.begin_nested():
                    listing, is_new = upsert_listing(session, adapter.id, raw, listing_pc)
                    changed = record_listing_price(session, listing, raw)
            except Exception as exc:  # one bad product must not stop the run
                stats.errors += 1
                if len(stats.messages) < 20:
                    stats.messages.append(f"{raw.chain_id}/{raw.chain_product_id}: {exc}")
                continue
            stats.products_checked += 1
            stats.new_listings += is_new
            stats.prices_changed += changed
            if stats.products_checked % COMMIT_EVERY == 0:
                session.commit()
        session.commit()
    except Exception as exc:
        session.rollback()
        stats.errors += 1
        stats.messages.append(f"catalog: {exc}")
        stats.status = "failed" if stats.products_checked == 0 else "partial"
        log.exception("source %s failed", adapter.id)


async def _backfill_eans(session, adapter, ctx, listing_pc, run, stats: RunStats, budget: int) -> None:  # type: ignore[no-untyped-def]
    """Fetch product details (with EAN) for listings that don't have one yet, a few per night."""
    if budget <= 0 or stats.status == "failed":
        return
    missing = session.exec(
        select(Listing)
        .where(
            Listing.source == adapter.id,
            Listing.postal_code == listing_pc,
            col(Listing.ean).is_(None),
            col(Listing.last_seen_at) >= run.started_at,
        )
        .order_by(col(Listing.id))
        .limit(budget)
    ).all()
    for listing in missing:
        try:
            raw = await adapter.fetch_product(ctx, listing.chain_product_id)
            if raw is None:
                continue
            with session.begin_nested():
                updated, _ = upsert_listing(session, adapter.id, raw, listing_pc)
                stats.prices_changed += record_listing_price(session, updated, raw)
                stats.eans_added += updated.ean is not None
        except Exception as exc:
            stats.errors += 1
            if len(stats.messages) < 20:
                stats.messages.append(f"detail {listing.chain_product_id}: {exc}")
    session.commit()
