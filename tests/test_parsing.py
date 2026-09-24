import pytest

from cardsniper.conditions import meets, normalize, normalize_cardmarket
from cardsniper.currency import Fx, parse_price


@pytest.mark.parametrize("text,expected", [
    ("£12.50", (12.5, "GBP")),
    ("12,50 €", (12.5, "EUR")),
    ("EUR 1.234,56", (1234.56, "EUR")),
    ("3 in stock £2.50", (2.5, "GBP")),
    ("£1,200", (1200.0, "GBP")),
    ("0,02 €", (0.02, "EUR")),
    ("", None),
])
def test_parse_price(text, expected):
    assert parse_price(text) == expected


@pytest.mark.parametrize("text,code", [
    ("Near Mint", "NM"), ("NM Foil", "NM"), ("Lightly played (Excellent)", "EX"),
    ("Lightly Played", "EX"), ("Slightly Played", "EX"), ("Moderately played (Very good)", "GD"),
    ("Heavily played (Poor)", "PL"), ("Played", "PL"), ("Damaged", "PO"), ("Mint", "MT"),
    ("Default Title", None), (None, None),
])
def test_normalize_condition(text, code):
    assert normalize(text) == code


def test_cardmarket_condition_keeps_lp_meaning():
    # on Cardmarket "LP" is Light Played (worse than EX), unlike shops
    assert normalize_cardmarket("LP") == "LP"
    assert normalize("LP") == "EX"


def test_meets():
    assert meets("NM", "EX")
    assert meets("EX", "EX")
    assert not meets("GD", "EX")
    assert not meets(None, "EX")


def test_fx():
    fx = Fx(0.85)
    assert fx.to_gbp(10, "EUR") == 8.5
    assert fx.to_gbp(10, "GBP") == 10
