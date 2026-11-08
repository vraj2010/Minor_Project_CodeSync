from ledger.parsing import parse_amount, parse_flag, parse_lines, parse_row


def test_parse_amount():
    assert parse_amount("$1,200.50") == 1200.5


def test_parse_flag():
    assert parse_flag("Yes")


def test_parse_row():
    item = parse_row("pen, 2.50, 3")
    assert (item.sku, item.price, item.quantity) == ("pen", 2.5, 3)


def test_parse_lines():
    assert len(parse_lines("pen,1\n\nink,2\n")) == 2
