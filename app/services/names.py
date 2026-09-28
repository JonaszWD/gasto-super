"""Clean product names for display and matching: what the product *is*, without pack size or store.

    "Garbanzo cocido Classic Carrefour pack de 3 latas de 400 g."  -> "Garbanzo cocido"
    "Pechuga de pollo corte fino Selección de Dia 600 g aprox."    -> "Pechuga de pollo corte fino"
    "Alubia cocida blanca Hacendado"                               -> "Alubia cocida blanca"

Removed: pack sizes and counts, packaging next to a size ("botella 1,5 l"), "aprox", and store names
with their own product lines (Carrefour Classic, Dia Al Punto, Hacendado...). Manufacturer brands
(Luengo, Milka) stay: they tell products apart. The size lives in quantity_value/quantity_unit.
"""

import re

from app.services.quantity import UNITS

# Store brands and store product lines, longest first so "Carrefour El Mercado" beats "Carrefour".
# Words like "Extra" or "Classic" are only removed next to the store name ("aceite virgen extra" stays).
STORE_LINES = [
    # Carrefour
    "Carrefour El Mercado", "Carrefour Classic'", "Carrefour Classic", "Classic' Carrefour", "Classic Carrefour",
    "Carrefour Extra", "Extra Carrefour", "Carrefour Sensation", "Sensation Carrefour", "Carrefour Bio",
    "Carrefour Essential", "Carrefour Original", "Carrefour Kids", "Carrefour Veggie", "Carrefour Selección",
    "Carrefour Discount", "Carrefour Baby", "Carrefour Soft", "Carrefour Expert", "Carrefour Home", "Carrefour",
    "El Mercado", "De Nuestra Tierra", "Simpl",
    # Dia
    "El Molino de Dia", "Selección de Dia", "Galleteca de Dia", "Iceberg de Dia", "Fiesta del Dia", "Aliña tu Dia",
    "Gran Dia", "Dia Selección Mundial", "Dia Saborfera", "Dia Fruticampo", "Dia Upss", "Dia Hola Cola",
    "Dia La Llama", "Dia Ramblers", "Dia Naturmundo", "Dia Funky Frank", "Dia Tetería", "Dia Al Diante", "Dia Al Punto",
    "Dia Temptation", "Dia Vegecampo", "Dia Cafetería", "Dia Super Paco",
    "Dia El diablo", "Dia Nuestra Alacena", "Dia Delicious", "Dia Láctea", "Dia Mari Marinera", "Dia Basic",
    "Dia Bonté", "Dia Babysmile", "Dia Baby", "Dia Pampa", "Dia Naturaleza", "Dia",
    # Mercadona
    "Mini & Go Hacendado", "Hacendado", "Deliplus", "Bosque Verde", "Compy",
    # Lidl, Aldi, Alcampo, Eroski
    "Milbona", "Alesto Selection", "Alesto", "Pikok", "Solevita", "Chef Select", "Deluxe Lidl", "Lidl",
    "Milsani", "Aldi", "Auchan", "Alcampo", "Eroski",
]
_STORE_RE = re.compile(
    r"(?i)(?<![\w'])(?:" + "|".join(re.escape(s) for s in sorted(STORE_LINES, key=len, reverse=True)) + r")(?![\w'])"
)

_NUM = r"\d+(?:[.,]\d+)?"
_UNIT = "|".join(sorted([*UNITS, "rollo", "briks", "bricks", "latas", "botellas", "sobres", "piezas"], key=len, reverse=True))
_PACKAGING = (
    r"(?:botella|botellas|lata|latas|brik|briks|brick|bricks|tarro|bote|bandeja|garrafa|malla|bolsa|caja|frasco"
    r"|tarrina|paquete|pack|envase)"
)
_COUNT = (
    r"(?:unidades|unidad|uds|ud|u|latas|botellas|briks|bricks|tarrinas|tarros|botes|bandejas|frascos|bolsas|cajas"
    r"|sobres|piezas|rollos|bolsitas)"
)
_SIZE_RE = re.compile(
    rf"""(?ix)
    (?:\b(?:en\s+)?{_PACKAGING}\s+(?:de\s+)?)?          # "pack de", "botella", "en lata"
    (?:\d+\s*{_COUNT}?\s*(?:de\s+|x\s*|×\s*))?        # "3 latas de", "6 x", "4x"
    {_NUM}\s*(?:{_UNIT})\b\.?                          # "400 g.", "1,5 l", "12 uds"
    |\b{_PACKAGING}\s+(?:de\s+)?\d+\b(?:\s*{_COUNT}\b)?  # "pack de 2", "pack 3 briks"
    |\baprox\b\.?
    """
)
_TRAILING = re.compile(r"(?i)(?:[\s,.;:\-–]|\b(?:de|del|en|con|y|x|pack|formato)\b)+$")


def clean_name(name: str, brand: str | None = None) -> str:
    """Product name without size, packaging, 'aprox' or store brand; keeps case and accents."""
    s = _SIZE_RE.sub(" ", name)
    s = _STORE_RE.sub(" ", s)
    if brand and brand.strip():
        b = re.escape(brand.strip().rstrip("®"))
        if _STORE_RE.fullmatch(brand.strip()):
            s = re.sub(rf"(?i)(?<!\w){b}(?!\w)", " ", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s+([,.;:])", r"\1", s)
    s = _TRAILING.sub("", s.strip()).strip(" ,.;:-")
    return s or name.strip()
