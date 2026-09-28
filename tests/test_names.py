import pytest

from app.services.matching import keywords
from app.services.names import clean_name


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Garbanzo cocido Classic Carrefour pack de 3 latas de 400 g.", "Garbanzo cocido"),
        ("Pechuga de pollo corte fino Selección de Dia 600 g aprox.", "Pechuga de pollo corte fino"),
        ("Alubia cocida blanca Hacendado", "Alubia cocida blanca"),
        ("Contra de pollo Carrefour El Mercado 600 g aprox", "Contra de pollo"),
        ("Pulpa de aguacate Dia Al Punto 125 g", "Pulpa de aguacate"),
        ("Salmón al natural alto en proteínas Dia mari marinera 2 x 50 g", "Salmón al natural alto en proteínas"),
        ("Cerveza Mahou Clásica pack de 12 botellas de 25 cl.", "Cerveza Mahou Clásica"),
        ("Coca Cola zero azúcar botella 1 l.", "Coca Cola zero azúcar"),
        ("Tomate frito Carrefour sin gluten pack de 2 tarros de 550 g.", "Tomate frito sin gluten"),
        ("Kéfir natural Nestlé sin gluten pack de 6 unidades de 100 g.", "Kéfir natural Nestlé sin gluten"),
        ("Bífidus con mango Dia 4 x 125 g", "Bífidus con mango"),
        ("Coca-Cola 12 x 330 ml", "Coca-Cola"),
        ("Papel de cocina Jumbo Roll Carrefour Essential 1 rollo.", "Papel de cocina Jumbo Roll"),
    ],
)
def test_clean_name_drops_size_and_store(raw: str, clean: str) -> None:
    assert clean_name(raw) == clean


@pytest.mark.parametrize(
    "name",
    [
        "Aceite de oliva virgen extra Ybarra",  # "extra" only goes next to the store name
        "Lenteja categoría extra Luengo",
        "Cerveza San Miguel 0,0",  # alcohol content, not a size
        "Vino blanco semidulce Diamante DO Rioja",  # "Dia" only as a whole word
    ],
)
def test_clean_name_keeps_meaningful_words(name: str) -> None:
    assert clean_name(name) == name


def test_clean_name_drops_store_brand_field_but_keeps_manufacturer() -> None:
    assert clean_name("Queso cheddar Lidl", "Lidl") == "Queso cheddar"
    assert clean_name("Mantequilla con sal De Nuestra Tierra 250 g.", "DE NUESTRA TIERRA") == "Mantequilla con sal"
    assert clean_name("Pasta letras Gallo 450 g.", "GALLO") == "Pasta letras Gallo"


def test_clean_name_never_returns_empty() -> None:
    assert clean_name("Hacendado") == "Hacendado"


def test_store_product_lines_are_not_keywords() -> None:
    assert keywords("Pasta estrellas Carrefour Classic 500 g.") == keywords("Pasta estrellas Hacendado")
