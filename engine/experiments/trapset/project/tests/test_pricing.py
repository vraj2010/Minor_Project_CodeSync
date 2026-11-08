from ledger.models import Invoice, Item
from ledger.pricing import (
    apply_discount,
    bulk_price,
    invoice_total,
    price_band,
    round_money,
    split_evenly,
    tax_for,
)


def test_round_money():
    assert round_money(2.5) == 2.5


def test_apply_discount():
    assert apply_discount(Item("pen", 10.0), 10).price == 9.0


def test_tax_for():
    assert tax_for(100) == 18.0


def test_invoice_total():
    assert invoice_total(Invoice("A").add(Item("pen", 100.0))) == 118.0


def test_split_evenly():
    assert split_evenly(10.0, 2) == [5.0, 5.0]


def test_bulk_price():
    assert bulk_price(10.0, 2) == (10.0, 20.0)


def test_price_band():
    assert price_band(50) == "low"
    assert price_band(5000) == "high"
