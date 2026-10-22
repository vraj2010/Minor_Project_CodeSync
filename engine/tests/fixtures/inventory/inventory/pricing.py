from .models import Item

TAX_RATE = 0.18


def price_with_tax(item, rate=TAX_RATE):
    return round(item.price * (1 + rate), 2)


def apply_discount(item: Item, percent):
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    return Item(item.sku, item.price * (100 - percent) / 100, item.quantity)
