from ledger.models import Invoice, Item
from ledger.report import render, summarize, top_items, totals_by_currency


def sample():
    return Invoice("A1").add(Item("pen", 2.0, 3)).add(Item("ink", 5.0))


def test_summarize():
    assert summarize(sample())["lines"] == 2


def test_top_items():
    assert top_items(sample(), 1)[0].sku == "pen"


def test_render():
    assert render(sample()).startswith("Invoice A1")


def test_totals_by_currency():
    assert totals_by_currency([sample()]) == {"USD": 11.0}
