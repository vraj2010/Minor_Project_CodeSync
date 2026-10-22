from dataclasses import dataclass


@dataclass
class Item:
    sku: str
    price: float
    quantity: int = 0

    def total(self):
        return self.price * self.quantity


class Stock:
    def __init__(self, items=None):
        self.items = list(items or [])

    def add(self, item):
        self.items.append(item)
        return self

    def value(self):
        return sum(item.total() for item in self.items)


class PerishableStock(Stock):
    def value(self):
        return super().value() * 0.9
