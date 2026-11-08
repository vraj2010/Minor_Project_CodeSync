import pytest

from ledger.errors import NotFoundError
from ledger.models import Invoice, Item
from ledger.store import AuditedRepository, Repository


def filled():
    return Repository([Invoice("B"), Invoice("A").add(Item("pen", 2.0))])


def test_get():
    assert filled().get("A").number == "A"


def test_get_missing():
    with pytest.raises(NotFoundError):
        filled().get("Z")


def test_numbers():
    assert filled().numbers() == ["A", "B"]


def test_delete():
    repository = filled()
    repository.delete("A")
    assert len(repository) == 1


def test_outstanding():
    assert filled().total_outstanding() == 2.0


def test_audit_log():
    repository = AuditedRepository()
    repository.save(Invoice("A"))
    assert repository.log == ["A"]
