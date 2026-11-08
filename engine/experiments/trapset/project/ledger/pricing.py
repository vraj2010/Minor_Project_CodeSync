from .errors import ValidationError
from .models import Invoice, Item

TAX_RATE = 0.18


def round_money(amount: float) -> float:
    return round(amount + 1e-9, 2)


def apply_discount(item: Item, percent: float) -> Item:
    if not 0 <= percent <= 100:
        raise ValidationError(f"bad percent {percent}")
    price = round_money(item.price * (100 - percent) / 100)
    return Item(item.sku, price, item.quantity)


def tax_for(amount: float, rate: float = TAX_RATE) -> float:
    return round_money(amount * rate)


def invoice_total(invoice: Invoice, rate: float = TAX_RATE) -> float:
    subtotal = invoice.subtotal()
    return round_money(subtotal + tax_for(subtotal, rate))


def split_evenly(amount: float, parts: int) -> list[float]:
    if parts < 1:
        raise ValidationError("parts must be at least 1")
    cents = round(amount * 100)
    share, extra = divmod(cents, parts)
    return [(share + (1 if index < extra else 0)) / 100 for index in range(parts)]


def bulk_price(price: float, quantity: int) -> tuple[float, float]:
    unit = price * 0.9 if quantity >= 10 else price
    return unit, round_money(unit * quantity)


def price_band(amount: float) -> str:
    if amount < 100:
        return "low"
    if amount < 1000:
        return "mid"
    return "high"
