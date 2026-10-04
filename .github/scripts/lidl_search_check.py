"""One-off: which way of reading Lidl finds its grocery products best?

A: the whole catalogue (q=*).
B: a search for each term in sources.toml (alcampo.search_terms) and each product type name.
C: the whole catalogue filtered to products sold in shops (store=1, "En tienda").

Food can't be told apart reliably yet (the `category` field is a path now), so every item is
saved as a slim record and classified afterwards. Results are written after each strategy, and
dropped connections are retried, so one failure doesn't lose the run. Output in docs/lidl-check/:
  items_A.jsonl, items_B.jsonl, items_C.jsonl   one slim record per item (B also has the term)
  raw_store_page.json                           first store=1 page, verbatim (fixture material)
  summary.md                                    requests, time and counts per strategy
"""

import json
import time
import tomllib
from collections import Counter
from pathlib import Path

import httpx

API = "https://www.lidl.es/q/api/search"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "es-ES,es;q=0.9",
}
OUT = Path("docs/lidl-check")
GAP = 1.0
summary: list[str] = ["# Lidl: three ways of reading the catalogue", ""]


def get(client: httpx.Client, params: dict, stats: dict) -> dict:
    for attempt in range(5):
        time.sleep(GAP if attempt == 0 else 2**attempt)
        stats["requests"] += 1
        try:
            r = client.get(API, params={"assortment": "ES", "locale": "es_ES", "version": "2.1.0", **params})
        except httpx.TransportError as exc:
            stats["retries"] += 1
            last = exc
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            stats["retries"] += 1
            last = RuntimeError(f"HTTP {r.status_code}")
            continue
        r.raise_for_status()
        return r.json()
    raise last


def walk(client, params: dict, size: int, stats: dict, max_pages: int, keep_first: list | None = None):
    offset = 0
    for _ in range(max_pages):
        data = get(client, {**params, "fetchsize": size, "offset": offset}, stats)
        if keep_first is not None and not keep_first:
            keep_first.append(data)
        items = data.get("items") or []
        yield from items
        offset += len(items)
        if not items or offset >= (data.get("numFound") or 0):
            return


def slim(item: dict, **extra) -> dict:
    d = (item.get("gridbox") or {}).get("data") or {}
    pr = d.get("price") or {}
    lp = next(((o.get("price") or {}) for o in d.get("lidlPlus") or [] if o.get("price")), {})
    return {
        **extra,
        "id": str(d.get("productId") or d.get("erpNumber") or ""),
        "title": d.get("fullTitle") or d.get("title"),
        "brand": (d.get("brand") or {}).get("name"),
        "category": d.get("category"),
        "analytics_category": (d.get("keyfacts") or {}).get("analyticsCategory"),
        "secondary": d.get("categorySecondaryPath"),
        "store": d.get("store"),
        "online": d.get("online"),
        "price": pr.get("price"),
        "old_price": pr.get("oldPrice"),
        "packaging": (pr.get("packaging") or {}).get("text"),
        "base_price": (pr.get("basePrice") or {}).get("text"),
        "lidl_plus_price": lp.get("price"),
        "price_start": pr.get("startDate"),
        "price_end": pr.get("endDate"),
        "store_start": d.get("storeStartDate"),
        "store_end": d.get("storeEndDate"),
        "zones": sorted((d.get("zones") or {}).keys()),
        "url": d.get("canonicalUrl"),
    }


def save(name: str, rows: list[dict]) -> None:
    with (OUT / name).open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def report(label: str, stats: dict, rows: list[dict], seconds: float) -> None:
    ids = {r["id"] for r in rows}
    cats = Counter((r["category"] or "").split("/")[1] if "/" in (r["category"] or "") else (r["category"] or "") for r in rows)
    summary.extend([
        f"## {label}", "",
        f"- Requests: {stats['requests']} ({stats['retries']} retries), time: {round(seconds)} s",
        f"- Items read: {len(rows)}, unique: {len(ids)}",
        f"- Top-level categories: {cats.most_common(12)}",
        f"- Sold in shops (store=true): {sum(1 for r in rows if r['store'])}; with packaging: "
        f"{sum(1 for r in rows if r['packaging'])}; with base price: {sum(1 for r in rows if r['base_price'])}",
        "",
    ])
    (OUT / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print("\n".join(summary[-8:]), flush=True)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    sources = tomllib.loads(Path("app/sources/sources.toml").read_text(encoding="utf-8"))
    types = tomllib.loads(Path("app/product_types.toml").read_text(encoding="utf-8"))
    terms = list(dict.fromkeys(
        list(sources["alcampo"]["search_terms"]) + [t["es"].lower() for t in types["types"].values()]
    ))
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        # C first: it's the cheapest and the most likely winner.
        stats, t0, first = {"requests": 0, "retries": 0}, time.time(), []
        rows = [slim(i) for i in walk(client, {"q": "*", "store": "1"}, 100, stats, 50, first)]
        save("items_C.jsonl", rows)
        (OUT / "raw_store_page.json").write_text(json.dumps(first[0] if first else {}, ensure_ascii=False, indent=1), encoding="utf-8")
        report("C: catalogue, sold in shops only (store=1)", stats, rows, time.time() - t0)

        stats, t0, rows, failed = {"requests": 0, "retries": 0}, time.time(), [], []
        for term in terms:
            try:
                rows += [slim(i, term=term) for i in walk(client, {"q": term}, 50, stats, 10)]
            except Exception as exc:  # noqa: BLE001 - one term failing must not lose the run
                failed.append(f"{term} ({exc})")
        save("items_B.jsonl", rows)
        report(f"B: {len(terms)} grocery term searches", stats, rows, time.time() - t0)
        if failed:
            summary.append(f"Failed terms: {', '.join(failed)}\n")

        stats, t0 = {"requests": 0, "retries": 0}, time.time()
        rows = []
        try:
            for i in walk(client, {"q": "*"}, 100, stats, 200):
                rows.append(slim(i))
        except Exception as exc:  # noqa: BLE001
            summary.append(f"A stopped early: {exc}\n")
        save("items_A.jsonl", rows)
        report("A: whole catalogue (q=*)", stats, rows, time.time() - t0)


if __name__ == "__main__":
    main()
