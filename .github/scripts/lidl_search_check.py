"""One-off: is searching Lidl for our common grocery terms better than reading the whole catalogue?

Strategy A: walk the full catalogue (q=*), keep food (category "Food" or "F+V").
Strategy B: search each term from sources.toml (alcampo.search_terms) and each product type name
            (app/product_types.toml), keep food the same way.
Writes docs/lidl-check/: summary.md, products.csv (every food product, which strategy found it),
raw_catalog_page.json (first catalogue page verbatim) and raw_food_items.json (food items verbatim).
"""

import csv
import json
import time
import tomllib
from pathlib import Path

import httpx

API = "https://www.lidl.es/q/api/search"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "es-ES,es;q=0.9",
}
FOOD = {"Food", "F+V"}
OUT = Path("docs/lidl-check")
GAP = 1.0


def page(client: httpx.Client, q: str, offset: int, size: int, stats: dict) -> dict:
    time.sleep(GAP)
    stats["requests"] += 1
    r = client.get(API, params={"q": q, "assortment": "ES", "locale": "es_ES", "version": "2.1.0",
                                "fetchsize": size, "offset": offset})
    r.raise_for_status()
    return r.json()


def walk(client: httpx.Client, q: str, size: int, stats: dict, max_pages: int, first: list | None = None):
    offset = 0
    for _ in range(max_pages):
        data = page(client, q, offset, size, stats)
        if first is not None and not first:
            first.append(data)
        items = data.get("items") or []
        for item in items:
            yield item
        offset += len(items)
        if not items or offset >= (data.get("numFound") or 0):
            return


def data_of(item: dict) -> dict:
    return (item.get("gridbox") or {}).get("data") or {}


def pid(d: dict) -> str:
    return str(d.get("productId") or d.get("erpNumber") or d.get("itemId") or d.get("fullTitle"))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    sources = tomllib.loads(Path("app/sources/sources.toml").read_text(encoding="utf-8"))
    types = tomllib.loads(Path("app/product_types.toml").read_text(encoding="utf-8"))
    terms = list(dict.fromkeys(
        list(sources["alcampo"]["search_terms"]) + [t["es"].lower() for t in types["types"].values()]
    ))
    food: dict[str, dict] = {}
    found_by: dict[str, set] = {}
    a = {"requests": 0, "items": 0, "food": 0}
    b = {"requests": 0, "items": 0, "food": 0, "nonfood": 0, "terms_with_food": 0}
    first: list = []
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        t0 = time.time()
        for item in walk(client, "*", 100, a, 200, first):
            a["items"] += 1
            d = data_of(item)
            if d.get("category") in FOOD:
                a["food"] += 1
                food.setdefault(pid(d), item)
                found_by.setdefault(pid(d), set()).add("A")
        a["seconds"] = round(time.time() - t0)
        (OUT / "raw_catalog_page.json").write_text(json.dumps(first[0] if first else {}, ensure_ascii=False, indent=1), encoding="utf-8")

        t0 = time.time()
        per_term = []
        for term in terms:
            n_food = 0
            for item in walk(client, term, 50, b, 10):
                b["items"] += 1
                d = data_of(item)
                if d.get("category") in FOOD:
                    n_food += 1
                    food.setdefault(pid(d), item)
                    found_by.setdefault(pid(d), set()).add("B")
                else:
                    b["nonfood"] += 1
            b["food"] += n_food
            b["terms_with_food"] += n_food > 0
            per_term.append((term, n_food))
        b["seconds"] = round(time.time() - t0)

    only_a = [k for k, v in found_by.items() if v == {"A"}]
    only_b = [k for k, v in found_by.items() if v == {"B"}]
    both = [k for k, v in found_by.items() if v == {"A", "B"}]
    (OUT / "raw_food_items.json").write_text(json.dumps(list(food.values()), ensure_ascii=False, indent=1), encoding="utf-8")

    with (OUT / "products.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["found_by", "id", "title", "brand", "category", "price", "old_price", "lidl_plus_price",
                    "packaging", "base_price", "store_start", "store_end", "url"])
        for k, item in sorted(food.items(), key=lambda kv: "".join(sorted(found_by[kv[0]]))):
            d = data_of(item)
            pr = d.get("price") or {}
            lp = next(((o.get("price") or {}).get("price") for o in d.get("lidlPlus") or [] if o.get("price")), None)
            w.writerow(["+".join(sorted(found_by[k])), k, d.get("fullTitle") or d.get("title"),
                        (d.get("brand") or {}).get("name"), d.get("category"), pr.get("price"), pr.get("oldPrice"), lp,
                        (pr.get("packaging") or {}).get("text"), (pr.get("basePrice") or {}).get("text"),
                        d.get("storeStartDate"), d.get("storeEndDate"), d.get("canonicalUrl")])

    datas = [data_of(i) for i in food.values()]
    lines = [
        "# Lidl: full catalogue vs searching our grocery terms", "",
        f"Unique food products found overall: {len(food)}", "",
        "| | Full catalogue (A) | Term searches (B) |", "|---|---|---|",
        f"| Requests | {a['requests']} | {b['requests']} |",
        f"| Time | {a['seconds']} s | {b['seconds']} s |",
        f"| Items read | {a['items']} | {b['items']} |",
        f"| Unique food products | {len(only_a) + len(both)} | {len(only_b) + len(both)} |",
        f"| Found only by this strategy | {len(only_a)} | {len(only_b)} |",
        "",
        f"- Found by both: {len(both)}",
        f"- Term searches: {len(terms)} terms, {b['terms_with_food']} returned any food; {b['nonfood']} non-food results read",
        f"- Food products with a price: {sum(1 for d in datas if (d.get('price') or {}).get('price') is not None)}; "
        f"with Lidl Plus price only: {sum(1 for d in datas if (d.get('price') or {}).get('price') is None and d.get('lidlPlus'))}",
        f"- With basePrice (Lidl's own unit price): {sum(1 for d in datas if (d.get('price') or {}).get('basePrice'))}; "
        f"with packaging text: {sum(1 for d in datas if ((d.get('price') or {}).get('packaging') or {}).get('text'))}",
        f"- With an old price (on offer): {sum(1 for d in datas if (d.get('price') or {}).get('oldPrice'))}; "
        f"with store dates: {sum(1 for d in datas if d.get('storeStartDate'))}",
        "", "## Food found per term (top 25)", "",
    ] + [f"- {t}: {n}" for t, n in sorted(per_term, key=lambda x: -x[1])[:25]] + [
        "", f"Terms with no food at all: {', '.join(t for t, n in per_term if n == 0)}",
    ]
    (OUT / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
