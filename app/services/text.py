import re
import unicodedata


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalize(s: str | None) -> str:
    """Lower-case, accent-free, single-spaced alphanumerics: 'Café Molido ' -> 'cafe molido'."""
    if not s:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", strip_accents(s).lower()).strip()


def search_text(*parts: str | None) -> str:
    return " ".join(p for p in (normalize(x) for x in parts) if p)[:400]
