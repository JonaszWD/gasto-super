import pytest

from app.services.quantity import Quantity, parse_quantity, unit_price_cents


@pytest.mark.parametrize(
    ("text", "value", "unit"),
    [
        ("500 g", 0.5, "kg"),
        ("250gr", 0.25, "kg"),
        ("1 kg", 1.0, "kg"),
        ("0,5 kg", 0.5, "kg"),
        ("aprox. 1,2 kg", 1.2, "kg"),
        ("1 L", 1.0, "l"),
        ("1.5L", 1.5, "l"),
        ("75 cl", 0.75, "l"),
        ("330 ml", 0.33, "l"),
        ("Garrafa 5 L", 5.0, "l"),
        ("6 x 1 L", 6.0, "l"),
        ("Leche 6x1l", 6.0, "l"),
        ("2x330ml", 0.66, "l"),
        ("Pack-6 x 33 cl", 1.98, "l"),
        ("6 botellas x 1,5 L", 9.0, "l"),
        ("pack 4 x 125 g", 0.5, "kg"),
        ("Yogur natural Dia Láctea 8 x 125 g", 1.0, "kg"),
        ("Pack 3 latas 80 g", 0.24, "kg"),
        ("Leche fermentada pack de 6 unidades de 100 g.", 0.6, "kg"),
        ("4 latas de 330 ml", 1.32, "l"),
        ("Bote 400 g (peso escurrido 240 g)", 0.4, "kg"),
        ("12 uds", 12.0, "unit"),
        ("Papel higiénico 12 rollos", 12.0, "unit"),
        ("Detergente 30 lavados", 30.0, "unit"),
        ("Huevos L docena", 12.0, "unit"),
        ("media docena", 6.0, "unit"),
        ("Pack de 6", 6.0, "unit"),
        ("Aceite 1 litro", 1.0, "l"),
        ("Queso 200 g", 0.2, "kg"),
    ],
)
def test_parse_quantity(text: str, value: float, unit: str) -> None:
    q = parse_quantity(text)
    assert q is not None, text
    assert q.unit == unit
    assert q.value == pytest.approx(value)


@pytest.mark.parametrize("text", [None, "", "Plátano de Canarias", "Pan de barra"])
def test_parse_quantity_none(text: str | None) -> None:
    assert parse_quantity(text) is None


def test_unit_price() -> None:
    assert unit_price_cents(1725, Quantity(5.0, "l")) == 345
    assert unit_price_cents(179, Quantity(0.5, "kg")) == 358
    assert unit_price_cents(250, Quantity(12, "unit")) == 21
    assert unit_price_cents(100, None) is None
    assert unit_price_cents(100, Quantity(0, "kg")) is None
