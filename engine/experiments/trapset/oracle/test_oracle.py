"""The hidden suite. It is never shown to the checks: it only establishes that
a trap really is a break, and that a control really is harmless."""

import asyncio
from datetime import date

import pytest

from ledger import app, dates, parsing, pricing, rates, report, store, text
from ledger.errors import NotFoundError, ValidationError
from ledger.models import Invoice, Item


def sample():
    return Invoice("A1").add(Item("pen", 2.0, 3)).add(Item("ink", 5.0))


# models


def test_item_total():
    assert Item("pen", 2.0, 3).total() == 6.0


def test_with_quantity_leaves_the_original_alone():
    item = Item("pen", 2.0, 3)
    other = item.with_quantity(9)
    assert (item.quantity, other.quantity) == (3, 9)
    assert other is not item


def test_add_chains():
    invoice = Invoice("A")
    assert invoice.add(Item("pen", 1.0)) is invoice
    assert app.chained_invoice("B", Item("a", 1.0), Item("b", 2.0)).count() == 2


def test_subtotal_and_count():
    assert sample().subtotal() == 11.0
    assert sample().count() == 4


def test_find_and_remove():
    invoice = sample()
    assert invoice.find("none") is None
    assert invoice.remove("pen").sku == "pen"
    with pytest.raises(NotFoundError):
        invoice.remove("pen")


def test_recurring_invoices():
    invoice = app.recurring("R", 2).add(Item("pen", 10.0))
    assert invoice.subtotal() == 20.0
    assert invoice.total_with(0.5) == 31.0
    assert app.billed_amounts([sample(), invoice], 0.0) == [11.0, 21.0]


# pricing


def test_round_money():
    assert pricing.round_money(1.005) == 1.01
    assert pricing.round_money(2.5) == 2.5


def test_apply_discount():
    item = pricing.apply_discount(Item("pen", 10.0, 2), 10)
    assert (item.sku, item.price, item.quantity) == ("pen", 9.0, 2)
    assert pricing.apply_discount(Item("pen", 10.0), percent=100).price == 0.0
    for percent in (-1, 101):
        with pytest.raises(ValidationError):
            pricing.apply_discount(Item("pen", 10.0), percent)


def test_discounted_invoice():
    assert app.discounted(sample(), 50).subtotal() == 5.5


def test_tax_for():
    assert pricing.tax_for(100) == 18.0
    assert pricing.tax_for(100, 0.1) == 10.0
    assert pricing.tax_for(amount=200, rate=0.5) == 100.0


def test_invoice_total():
    invoice = Invoice("A").add(Item("pen", 100.0))
    assert pricing.invoice_total(invoice) == 118.0
    assert pricing.invoice_total(invoice, 0.5) == 150.0


def test_split_evenly():
    assert pricing.split_evenly(10.0, 3) == [3.34, 3.33, 3.33]
    assert isinstance(pricing.split_evenly(1.0, 1), list)
    assert app.shares(1.0, 4) == 4
    with pytest.raises(ValidationError):
        pricing.split_evenly(1.0, 0)


def test_bulk_price():
    assert pricing.bulk_price(10.0, 2) == (10.0, 20.0)
    assert app.bulk_summary(10.0, 10) == "10 x 9.00 = 90.00"


def test_price_band():
    assert pricing.price_band(99.99) == "low"
    assert pricing.price_band(100) == "mid"
    assert pricing.price_band(1000) == "high"
    assert app.band_label(5) == "LOW"


# parsing


def test_parse_amount():
    assert parsing.parse_amount("$1,200.50") == 1200.5
    with pytest.raises(ValidationError):
        parsing.parse_amount("abc")
    assert app.safe_amount("abc", 7.0) == 7.0


def test_parse_flag():
    assert parsing.parse_flag(" Yes ") is True
    assert parsing.parse_flag("no") is False
    assert app.enabled("y") == "on"


def test_parse_row():
    item = parsing.parse_row("pen, 2.50, 3")
    assert isinstance(item, Item)
    assert (item.sku, item.price, item.quantity) == ("pen", 2.5, 3)
    assert parsing.parse_row("pen;2", sep=";").quantity == 1
    assert parsing.parse_row("pen|2", "|").price == 2.0
    with pytest.raises(ValidationError):
        parsing.parse_row("pen,1", strict=True)
    with pytest.raises(ValidationError):
        parsing.parse_row("pen")


def test_parse_lines():
    assert len(parsing.parse_lines("pen,1\n\n# note\nink,2\n")) == 2
    assert len(parsing.parse_lines("pen;1", sep=";")) == 1
    with pytest.raises(ValidationError):
        parsing.parse_lines("pen,1\nbroken\n")


def test_iter_rows():
    assert app.first_row(["pen,1", "ink,2"]).sku == "pen"
    assert [item.sku for item in parsing.iter_rows(["a;1", "b;2"], ";")] == ["a", "b"]


# store


def test_get():
    repository = store.Repository([Invoice("A")])
    assert repository.get("A").number == "A"
    with pytest.raises(NotFoundError):
        repository.get("Z")


def test_pay_something_missing():
    assert app.pay(store.Repository(), "Z") is False


def test_a_bad_record_is_not_reported_as_missing():
    class Broken(store.Repository):
        def get(self, number):
            raise ValidationError("corrupt")

    with pytest.raises(ValidationError):
        app.pay(Broken(), "A")


def test_save_keeps_the_same_object():
    repository = store.Repository()
    invoice = Invoice("A")
    assert repository.save(invoice) is invoice
    invoice.paid = True
    assert repository.get("A").paid


def test_find_with_a_default():
    repository = store.Repository()
    assert repository.find("A") is None
    assert repository.find("A", "fallback") == "fallback"
    assert app.lookup_or_new(repository, "N").number == "N"


def test_delete_says_what_happened():
    repository = store.Repository([Invoice("A")])
    assert repository.delete("A") is True
    assert repository.delete("A") is False
    assert app.remove_if_present(repository, "A") == "missing"


def test_numbers_are_sorted():
    repository = store.Repository([Invoice("B"), Invoice("A")])
    assert repository.numbers() == ["A", "B"]


def test_outstanding_leaves_out_what_is_paid():
    paid = Invoice("B", paid=True).add(Item("x", 9.0))
    repository = store.Repository([Invoice("A").add(Item("pen", 2.0)), paid])
    assert repository.total_outstanding() == 2.0
    assert [item.number for item in repository.unpaid()] == ["A"]


def test_audit_logs_are_separate():
    first = store.AuditedRepository()
    first.save(Invoice("A"))
    assert store.AuditedRepository().log == []
    assert first.log == ["A"]


# text


def test_slugify():
    assert text.slugify("  Hello,  World! ") == "hello-world"


def test_truncate():
    assert text.truncate("short", 10) == "short"
    assert text.truncate("a long sentence", 8) == "a lon..."
    assert len(text.truncate("a long sentence", 8)) == 8
    assert app.short_title("a very long title") == "a very l.."


def test_initials():
    assert text.initials("ada lovelace") == "AL"


# dates


def test_due_date():
    assert dates.due_date(date(2026, 1, 1)) == date(2026, 1, 31)


def test_is_overdue():
    assert dates.is_overdue(date(2026, 1, 1), date(2026, 2, 1)) is True
    assert dates.is_overdue(date(2026, 2, 1), date(2026, 1, 1)) is False
    assert dates.is_overdue(date(2026, 1, 1), date(2026, 1, 1)) is False


def test_days_between():
    assert dates.days_between(date(2026, 1, 10), date(2026, 1, 1)) == 9


def test_quarter_of():
    expected = [1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4, 4]
    assert [dates.quarter_of(date(2026, m, 1)) for m in range(1, 13)] == expected


# report


def test_summarize():
    assert report.summarize(sample()) == {
        "number": "A1",
        "lines": 2,
        "total": 12.98,
        "paid": False,
    }
    assert app.describe(sample()) == "A1: 12.98 (open)"


def test_top_items():
    invoice = Invoice("Y").add(Item("cheap", 1.0)).add(Item("dear", 9.0))
    original = list(invoice.items)
    assert [item.sku for item in report.top_items(invoice, 1)] == ["dear"]
    assert invoice.items == original
    assert len(report.top_items(Invoice("Z", [Item(str(n), 1.0) for n in range(9)]))) == 3


def test_format_line():
    assert report.format_line(Item("pen", 2.0, 3)) == "pen            3     6.00"


def test_render():
    assert report.render(sample()).splitlines()[-1] == "Total 12.98"
    assert len(report.render(sample()).splitlines()) == 4


def test_totals_by_currency():
    euro = Invoice("E", currency="EUR").add(Item("x", 1.0))
    usd = [Invoice("A").add(Item("x", 1.0)), Invoice("B").add(Item("x", 2.0))]
    assert report.totals_by_currency([*usd, euro]) == {"USD": 3.0, "EUR": 1.0}


def test_aging():
    invoices = [Invoice("A", paid=True), Invoice("B")]
    assert report.aging(invoices) == (1, 1)
    assert app.paid_ratio(invoices) == 0.5


# rates


def test_load_rates():
    assert rates.load_rates()["EUR"] == 0.5
    assert app.rate_table() == ["EUR", "INR", "USD"]


def test_fetch_rate_is_awaitable():
    assert asyncio.run(rates.fetch_rate("EUR", {"EUR": 2.0})) == 2.0


def test_convert_all():
    assert asyncio.run(rates.convert_all([2.0], "EUR", {"EUR": 2.0})) == [4.0]
    assert app.converted_total([2.0], "EUR") == 1.0


# app


def test_import_invoice():
    assert app.import_invoice("A", "pen,1\nink,2\n").count() == 2


def test_pay():
    repository = store.Repository([Invoice("A")])
    assert app.pay(repository, "A") is True
    assert repository.get("A").paid
