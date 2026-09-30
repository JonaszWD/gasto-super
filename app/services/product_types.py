"""Product types: generic names ("Pechuga de pollo") that group specific products across chains.

The types and their rules live in app/product_types.toml. Classification only looks at a
product's name and category, so it runs in memory: when the collectors create a canonical
product and in `python -m collectors retype`. The read side (services/compare.py) groups
listings by CanonicalProduct.product_type.
"""

import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.services.names import clean_name
from app.services.synonyms import Group, relevance, text_matches
from app.services.text import normalize

TYPES_PATH = Path(__file__).resolve().parent.parent / "product_types.toml"
MAX_GAP = 3  # words a "~" in a pattern may skip

Pattern = list[str]  # tokens: "word", "word*" (prefix) or "~" (gap)


@dataclass(frozen=True)
class ProductType:
    slug: str
    es: str
    en: str
    match: tuple[tuple[str, ...], ...]
    exclude: tuple[tuple[str, ...], ...]
    exclude_categories: tuple[str, ...]
    default_amount: float = 1.0  # shopping-list amount, in the unit the type is priced in


def _expand(values: list[str], sets: dict[str, list[str]]) -> list[str]:
    """"@name" entries are replaced by the shared list [sets].name."""
    out: list[str] = []
    for v in values:
        out.extend(sets[v[1:]] if v.startswith("@") else [v])
    return out


def _pattern(text: str) -> tuple[str, ...]:
    # Same normalisation as the names, but keep the "*" and "~" markers.
    return tuple(tok if tok == "~" else (normalize(tok) + "*" if tok.endswith("*") else normalize(tok)) for tok in text.split())


@lru_cache
def load_types(path: Path = TYPES_PATH) -> dict[str, ProductType]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    sets: dict[str, list[str]] = data.get("sets", {})
    global_cats = [normalize(c) for c in data.get("exclude_categories", [])]
    types: dict[str, ProductType] = {}
    for slug, t in data["types"].items():
        types[slug] = ProductType(
            slug=slug,
            es=t["es"],
            en=t["en"],
            match=tuple(_pattern(m) for m in t["match"]),
            exclude=tuple(_pattern(x) for x in _expand(t.get("exclude", []), sets)),
            exclude_categories=tuple(global_cats + [normalize(c) for c in _expand(t.get("exclude_categories", []), sets)]),
            default_amount=float(t.get("amount", 1)),
        )
    return types


def _token_hit(token: str, word: str) -> bool:
    return word.startswith(token[:-1]) if token.endswith("*") else word == token


def _match_at(tokens: tuple[str, ...], words: list[str], i: int) -> bool:
    """Do the tokens match the words starting at position i? (What follows is free.)"""
    if not tokens:
        return True
    tok, rest = tokens[0], tokens[1:]
    if tok == "~":
        return any(_match_at(rest, words, i + skip) for skip in range(MAX_GAP + 1) if i + skip <= len(words))
    return i < len(words) and _token_hit(tok, words[i]) and _match_at(rest, words, i + 1)


def _contains(tokens: tuple[str, ...], words: list[str]) -> bool:
    return any(_match_at(tokens, words, i) for i in range(len(words)))


def classify(name: str, brand: str | None = None, category: str | None = None, department: str | None = None) -> str | None:
    """The slug of the first type whose rules match this product, or None."""
    if department == "household":
        return None
    words = normalize(clean_name(name, brand)).split()
    if not words:
        return None
    cat = normalize(category)
    for pt in load_types().values():
        if not any(_match_at(m, words, 0) for m in pt.match):
            continue
        if any(_contains(x, words) for x in pt.exclude):
            continue
        if cat and any(c in cat for c in pt.exclude_categories):
            continue
        return pt.slug
    return None


def types_for_query(groups: list[Group]) -> list[ProductType]:
    """Types whose Spanish or English name matches a search ("chicken" -> every chicken type).

    Best match first; ties keep the file order (fresh cuts before the rest)."""
    if not groups:
        return []
    found = [pt for pt in load_types().values() if text_matches(f"{pt.es} {pt.en}", groups)]
    order = {slug: i for i, slug in enumerate(load_types())}
    score = {pt.slug: max(relevance(pt.es, None, groups), relevance(pt.en, None, groups)) for pt in found}
    return sorted(found, key=lambda pt: (-score[pt.slug], order[pt.slug]))
