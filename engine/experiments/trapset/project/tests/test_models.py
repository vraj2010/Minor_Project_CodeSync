from ledger.models import Invoice, Item, RecurringInvoice


def sample():
    return Invoice("A1").add(Item("pen", 2.0, 3)).add(Item("ink", 5.0))


def test_item_total():
    assert Item("pen", 2.0, 3).total() == 6.0


def test_subtotal():
    assert sample().subtotal() == 11.0


def test_count():
    assert sample().count() == 4


def test_find():
    assert sample().find("ink").price == 5.0


def test_remove():
    invoice = sample()
    assert invoice.remove("pen").sku == "pen"
    assert invoice.find("pen") is None


def test_recurring_subtotal():
    invoice = RecurringInvoice("R1", cycles=2).add(Item("pen", 2.0))
    assert invoice.subtotal() == 4.0
