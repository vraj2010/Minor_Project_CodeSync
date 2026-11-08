from .errors import NotFoundError
from .models import Invoice


class Repository:
    def __init__(self, invoices=None):
        self._invoices = {}
        for invoice in invoices or []:
            self.save(invoice)

    def __len__(self) -> int:
        return len(self._invoices)

    def save(self, invoice: Invoice) -> Invoice:
        self._invoices[invoice.number] = invoice
        return invoice

    def get(self, number: str) -> Invoice:
        try:
            return self._invoices[number]
        except KeyError:
            raise NotFoundError(number) from None

    def find(self, number: str, default=None):
        return self._invoices.get(number, default)

    def delete(self, number: str) -> bool:
        return self._invoices.pop(number, None) is not None

    def numbers(self) -> list[str]:
        return sorted(self._invoices)

    def unpaid(self) -> list[Invoice]:
        return [item for item in self._invoices.values() if not item.paid]

    def total_outstanding(self) -> float:
        return sum(item.subtotal() for item in self.unpaid())


class AuditedRepository(Repository):
    def __init__(self, invoices=None, log=None):
        self.log = [] if log is None else log
        super().__init__(invoices)

    def save(self, invoice: Invoice) -> Invoice:
        self.log.append(invoice.number)
        return super().save(invoice)
