"""Database size and row counts per table.

    uv run python -m app.tools.db_report            # plain text
    uv run python -m app.tools.db_report --markdown # for the GitHub Actions run summary
"""

import argparse
import os
from dataclasses import dataclass

from sqlalchemy import Engine, text

from app.config import get_settings
from app.db import make_engine

NEON_FREE_BYTES = 512 * 1024 * 1024


@dataclass
class TableSize:
    name: str
    rows: int
    total_bytes: int


def collect(engine: Engine) -> tuple[int, list[TableSize]]:
    with engine.connect() as conn:
        db_bytes = conn.execute(text("SELECT pg_database_size(current_database())")).scalar_one()
        tables = conn.execute(
            text(
                "SELECT c.relname, pg_total_relation_size(c.oid) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relkind = 'r' AND n.nspname = current_schema() ORDER BY 2 DESC"
            )
        ).all()
        out = []
        for name, size in tables:
            rows = conn.execute(text(f'SELECT count(*) FROM "{name}"')).scalar_one()
            out.append(TableSize(name, int(rows), int(size)))
    return int(db_bytes), out


def human(n: float) -> str:
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def render(db_bytes: int, tables: list[TableSize], markdown: bool) -> str:
    pct = db_bytes / NEON_FREE_BYTES * 100
    if markdown:
        lines = [
            "### Database size",
            f"**{human(db_bytes)}** ({pct:.1f}% of Neon's 0.5 GB free tier)",
            "",
            "| Table | Rows | Size |",
            "|---|---:|---:|",
        ]
        lines += [f"| {t.name} | {t.rows:,} | {human(t.total_bytes)} |" for t in tables]
    else:
        lines = [f"Database size: {human(db_bytes)} ({pct:.1f}% of 0.5 GB)", f"{'table':<24}{'rows':>12}{'size':>12}"]
        lines += [f"{t.name:<24}{t.rows:>12,}{human(t.total_bytes):>12}" for t in tables]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args()
    engine = make_engine(get_settings().database_url)
    output = render(*collect(engine), markdown=args.markdown)
    print(output)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if args.markdown and summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(output + "\n")


if __name__ == "__main__":
    main()
