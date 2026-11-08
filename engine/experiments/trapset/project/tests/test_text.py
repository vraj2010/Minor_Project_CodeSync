from datetime import date

from ledger.dates import due_date, is_overdue
from ledger.text import slugify


def test_slugify():
    assert slugify("Hello World") == "hello-world"


def test_due_date():
    assert due_date(date(2026, 1, 1), 10) == date(2026, 1, 11)


def test_is_overdue():
    assert is_overdue(date(2026, 1, 1), date(2026, 2, 1))
