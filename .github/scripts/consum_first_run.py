"""One-off check of the Consum source against the live shop, with no database.

Walks the catalogue exactly like ConsumAdapter.fetch_catalog, but also keeps what the adapter
drops, and writes everything to docs/consum-first-run/ for review:
  summary.md          counts, departments, sanity checks
  products.csv        every product the collector would store
  skipped.csv         products left out, and why
  categories.csv      every category name seen, with the department it ends up in
  raw_page_0.json     the first catalogue page, verbatim (to replace the test fixtures)
  raw_product.json    one product from the "refresh now" endpoint, verbatim
"""

import asyncio
import csv
import json
import os
import sys
from collections import Counter
from pathlib import Path

from app.sources.base import PoliteClient, SourceError
from app.sources.consum import BASE, HEADERS, MAX_PAGES, PAGE_SIZE, ConsumAdapter, parse_product
from app.sources.registry import build_adapter

OUT = Path("docs/consum-first-run")


def euros(cents: int | None) -> str:
    return "" if cents is None else f"{cents / 100:.2f}".replace(".", ",")


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    adapter = build_adapter("consum")
    assert isinstance(adapter, ConsumAdapter)
    http = PoliteClient(
        user_agent=os.environ.get("SOURCES_USER_AGENT") or "GastoSuper/0.1 (personal price comparison)",
        min_interval=adapter.min_interval,
    )
    products, skipped, raw_first = [], [], None
    categories: Counter[tuple[str, str]] = Counter()
    error, total = None, None
    offset = 0
    try:
        for _ in range(MAX_PAGES):
            resp = await http.request(
                "GET", f"{BASE}/catalog/product",
                params={"limit": PAGE_SIZE, "offset": offset, "showRecommendations": "false"}, headers=HEADERS,
            )
            if resp.status_code >= 400:
                raise SourceError(f"HTTP {resp.status_code} at offset {offset}: {resp.text[:300]!r}")
            data = resp.json()
            if raw_first is None:
                raw_first = data
                (OUT / "raw_page_0.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            total = data.get("totalCount")
            page = data.get("products") or []
            for p in page:
                cat_names = " / ".join(c.get("name") or "" for c in p.get("categories") or [])
                dept = adapter.department_for(p)
                for c in p.get("categories") or []:
                    categories[(c.get("name") or "", dept or "SKIPPED")] += 1
                name = ((p.get("productData") or {}).get("name") or "")
                if dept is None:
                    skipped.append([p.get("code"), name, cat_names, "non-grocery category"])
                    continue
                listing = parse_product(p, dept)
                if listing is None:
                    skipped.append([p.get("code"), name, cat_names, "no price or name"])
                    continue
                q = listing.resolved_quantity()
                products.append([
                    listing.chain_product_id, listing.name, listing.brand or "", listing.ean or "",
                    euros(listing.price_cents), euros(listing.resolved_unit_price()),
                    f"{q.value:g} {q.unit}" if q else "", listing.size_text or "",
                    (p.get("priceData") or {}).get("unitPriceUnitType") or "",
                    listing.department, cat_names, listing.url or "",
                ])
            offset += len(page)
            if not page or not data.get("hasMore", offset < (total or 0)):
                break
        if products:
            resp = await http.request("GET", f"{BASE}/catalog/product/code/{products[0][0]}", headers=HEADERS)
            body = resp.json() if resp.status_code < 400 else {"status": resp.status_code, "body": resp.text[:500]}
            (OUT / "raw_product.json").write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
    except (SourceError, ValueError) as exc:
        error = str(exc)
    finally:
        await http.aclose()

    def write_csv(name: str, header: list[str], rows: list[list]) -> None:
        with (OUT / name).open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)

    write_csv("products.csv", ["code", "name", "brand", "ean", "price_eur", "unit_price_eur", "quantity",
                               "format", "consum_unit_type", "department", "categories", "url"], products)
    write_csv("skipped.csv", ["code", "name", "categories", "reason"], skipped)
    write_csv("categories.csv", ["category", "department", "products"],
              [[c, d, n] for (c, d), n in sorted(categories.items(), key=lambda x: -x[1])])

    depts = Counter(r[9] for r in products)
    with_ean = sum(1 for r in products if r[3])
    with_unit = sum(1 for r in products if r[5])
    prices = sorted(int(r[4].replace(",", "")) for r in products)
    lines = [
        "# Consum: first live run", "",
        f"- Requests: {http.request_count}",
        f"- Status: {'FAILED: ' + error if error else 'ok'}",
        f"- Catalogue size reported by Consum: {total}",
        f"- Products kept: {len(products)} (food {depts['food']}, drink {depts['drink']}, household {depts['household']})",
        f"- Skipped: {len(skipped)} ({Counter(r[3] for r in skipped).most_common()})",
        f"- With EAN: {with_ean}; with a pack size: {sum(1 for r in products if r[6])}; with a unit price: {with_unit}",
        f"- Unit prices by unit: {Counter(r[6].split()[-1] for r in products if r[5] and r[6]).most_common()}",
    ]
    if prices:
        mid = prices[len(prices) // 2]
        lines += [
            f"- Pack price: min {euros(prices[0])} €, median {euros(mid)} €, max {euros(prices[-1])} €",
            "  (a median under 0,10 € would mean the API returns cents, not euros)",
        ]
    lines += ["", "Category names and where they end up: `categories.csv`. Kept products: `products.csv`."]
    (OUT / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 1 if error and not products else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
