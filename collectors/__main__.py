"""Price collectors CLI. Runs the same on a laptop, in Docker and in GitHub Actions.

    uv run python -m collectors list
    uv run python -m collectors run --chain mercadona          # chain-specific sources for Mercadona
    uv run python -m collectors run --source openprices        # one source (Open Prices covers many chains)
    uv run python -m collectors run --all
    uv run python -m collectors run --chain mercadona --limit 50   # quick local test
    uv run python -m collectors import-xlsx FILE... [--dry-run]    # one-off catalogue export import
    uv run python -m collectors retype                             # re-apply app/product_types.toml

Uses DATABASE_URL (in production: Neon's *direct* connection string).
Exit code 1 when a source failed completely, so the CI job shows red; partial runs exit 0.
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime

from app.config import get_settings
from sqlalchemy import Engine
from sqlmodel import Session

from app.db import make_engine
from app.services.catalog import retype
from app.sources.registry import ALL_SOURCES, build_adapter, enabled_sources, load_config, sources_for_chain
from collectors.runner import RunStats, run_source


def summary_markdown(results: list[RunStats]) -> str:
    lines = [
        "### Price collection",
        "",
        "| Source | Chains | Postal code | Status | Products checked | Prices changed | New listings | EANs added | Errors | Requests |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        lines.append(
            f"| {r.source} | {', '.join(r.chains)} | {r.postal_code} | {r.status} | {r.products_checked:,} | "
            f"{r.prices_changed:,} | {r.new_listings:,} | {r.eans_added:,} | {r.errors} | {r.requests} |"
        )
    notes = [f"- **{r.source}**: {m}" for r in results for m in r.messages[:10]]
    if notes:
        lines += ["", "<details><summary>Messages</summary>", "", *notes, "", "</details>"]
    return "\n".join(lines) + "\n"


def run_retype(engine: Engine) -> int:
    with Session(engine) as session:
        changed = retype(session)
        session.commit()
    print(f"product types: {changed} products changed")
    return changed


def cmd_list() -> int:
    config = load_config()
    enabled = set(enabled_sources(config))
    for source_id in ALL_SOURCES:
        adapter = build_adapter(source_id, config)
        assert adapter
        state = "enabled " if source_id in enabled else "disabled"
        print(f"{source_id:<12} {state}  chains: {', '.join(adapter.chains)}")
    disabled_chains = [k for k, v in config.items() if k not in ALL_SOURCES and not v.get("enabled", True)]
    for chain in disabled_chains:
        print(f"{chain:<12} no source (disabled in sources.toml)")
    return 0


async def cmd_run(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    jobs: list[tuple[str, set[str] | None]] = []
    if args.all:
        jobs = [(s, None) for s in enabled_sources()]
    for source_id in args.source or []:
        if source_id not in enabled_sources():
            print(f"source {source_id!r} is not enabled in app/sources/sources.toml", file=sys.stderr)
            return 2
        jobs.append((source_id, None))
    for chain in args.chain or []:
        found = sources_for_chain(chain)
        if not found:
            print(f"No enabled source for chain {chain!r}; nothing to do.")
        jobs.extend((s, {chain}) for s in found)

    results: list[RunStats] = []
    # Sequential on purpose: never more than one request in flight per chain.
    for source_id, chains in jobs:
        print(f"==> {source_id} {sorted(chains) if chains else ''}", flush=True)
        try:
            stats = await run_source(
                engine, source_id, chains=chains, postal_code=args.postal_code, limit=args.limit, max_detail=args.max_detail
            )
        except Exception as exc:  # isolate sources from each other
            logging.exception("source %s crashed", source_id)
            stats = RunStats(source_id, sorted(chains or []), args.postal_code or "", status="failed", errors=1, messages=[str(exc)])
        results.append(stats)
        print(
            f"    {stats.status}: {stats.products_checked} checked, {stats.prices_changed} changed, "
            f"{stats.new_listings} new, {stats.eans_added} EANs, {stats.errors} errors, {stats.requests} requests"
        )
        for m in stats.messages[:10]:
            print(f"    ! {m}")
    # Cheap (one read, one batched update); picks up rule changes in app/product_types.toml.
    try:
        run_retype(engine)
    except Exception:
        logging.exception("retype failed")
    engine.dispose()

    if results:
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(summary_markdown(results))
    return 1 if any(r.status == "failed" for r in results) else 0


def cmd_import_xlsx(args: argparse.Namespace) -> int:
    from pathlib import Path

    from collectors import xlsx_import

    observed_at = None
    if args.observed_at:
        observed_at = datetime.fromisoformat(args.observed_at)
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=xlsx_import.MADRID)
    try:
        listings, parse_stats = xlsx_import.parse_files(
            [Path(f) for f in args.files], set(args.chain) if args.chain else None, observed_at
        )
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.limit is not None:
        listings = listings[: args.limit]
    print(xlsx_import.describe(listings, parse_stats))
    if args.dry_run:
        return 0
    engine = make_engine(get_settings().database_url)
    stats = xlsx_import.import_listings(engine, listings, parse_stats)
    engine.dispose()
    print(
        f"{stats.status}: {stats.products_checked} imported, {stats.prices_changed} prices changed, "
        f"{stats.new_listings} new, {stats.eans_added} with EAN from other sources, {stats.errors} errors"
    )
    for m in stats.messages[:10]:
        print(f"    ! {m}")
    return 1 if stats.status == "failed" else 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="collectors", description="Price collectors")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show sources and their chains")
    run = sub.add_parser("run", help="collect prices into the database")
    run.add_argument("--chain", action="append", help="chain id, e.g. mercadona (repeatable)")
    run.add_argument("--source", action="append", choices=ALL_SOURCES, help="source id (repeatable)")
    run.add_argument("--all", action="store_true", help="every enabled source")
    run.add_argument("--postal-code", help="override the postal code stored in settings")
    run.add_argument("--limit", type=int, help="stop after N products per source (testing)")
    run.add_argument("--max-detail", type=int, help="override max product-detail fetches (EAN backfill)")
    sub.add_parser("report", help="database size and row counts")
    sub.add_parser("retype", help="re-apply app/product_types.toml to every product")
    imp = sub.add_parser("import-xlsx", help="import Carrefour/Dia catalogue exports (.xlsx)")
    imp.add_argument("files", nargs="+", help="xlsx files (Id, Nombre, Precio, Precio Pack, Formato, ...)")
    imp.add_argument("--chain", action="append", choices=["carrefour", "dia"], help="only this chain (repeatable)")
    imp.add_argument("--observed-at", help="price date, ISO (default: DDMMYYYY-HHMMSS in the file name, Madrid)")
    imp.add_argument("--limit", type=int, help="import only the first N products (testing)")
    imp.add_argument("--dry-run", action="store_true", help="parse and report, don't touch the database")
    args = parser.parse_args(argv)

    if args.command == "list":
        return cmd_list()
    if args.command == "report":
        from app.tools import db_report

        sys.argv = [sys.argv[0], "--markdown"] if os.environ.get("GITHUB_STEP_SUMMARY") else [sys.argv[0]]
        db_report.main()
        return 0
    if args.command == "import-xlsx":
        return cmd_import_xlsx(args)
    if args.command == "retype":
        engine = make_engine(get_settings().database_url)
        run_retype(engine)
        engine.dispose()
        return 0
    if not (args.all or args.chain or args.source):
        parser.error("run needs --chain, --source or --all")
    return asyncio.run(cmd_run(args))


if __name__ == "__main__":
    sys.exit(main())
