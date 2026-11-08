from dataclasses import dataclass, field

from .errors import NotFoundError

DEFAULT_CURRENCY = "USD"


@dataclass
class Item:
    sku: str
    price: float
    quantity: int = 1

    def total(self) -> float:
        return self.price * self.quantity

    def with_quantity(self, quantity: int) -> "Item":
        return Item(self.sku, self.price, quantity)


@dataclass
class Invoice:
    number: str
    items: list[Item] = field(default_factory=list)
    currency: str = DEFAULT_CURRENCY
    paid: bool = False

    def add(self, item: Item) -> "Invoice":
        self.items.append(item)
        return self

    def subtotal(self) -> float:
        return sum(item.total() for item in self.items)

    def total_with(self, rate: float) -> float:
        return self.subtotal() * (1 + rate)

    def count(self) -> int:
        return sum(item.quantity for item in self.items)

    def find(self, sku: str) -> Item | None:
        for item in self.items:
            if item.sku == sku:
                return item
        return None

    def remove(self, sku: str) -> Item:
        item = self.find(sku)
        if item is None:
            raise NotFoundError(sku)
        self.items.remove(item)
        return item


@dataclass
class RecurringInvoice(Invoice):
    cycles: int = 1

    def subtotal(self) -> float:
        return super().subtotal() * self.cycles

    def total_with(self, rate: float) -> float:
        return super().total_with(rate) + 1.0
