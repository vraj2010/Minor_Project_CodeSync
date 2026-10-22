from inventory import Stock
from inventory.pricing import price_with_tax as gross

from . import pricing


def summary(items):
    stock = Stock(items)
    lines = [f"{item.sku}: {gross(item)}" for item in items]
    lines.append(f"total: {stock.value()}")
    return "\n".join(lines)


def discounted(items, percent):
    return [pricing.apply_discount(item, percent) for item in items]
