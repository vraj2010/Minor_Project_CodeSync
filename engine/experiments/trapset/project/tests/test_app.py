from ledger.app import import_invoice, pay
from ledger.models import Invoice
from ledger.store import Repository


def test_import_invoice():
    assert import_invoice("A", "pen,1\nink,2\n").count() == 2


def test_pay():
    repository = Repository([Invoice("A")])
    assert pay(repository, "A") is True
    assert repository.get("A").paid
