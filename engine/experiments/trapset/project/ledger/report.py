from .models import Invoice, Item
from .pricing import invoice_total as total_of
from .text import truncate


def summarize(invoice: Invoice) -> dict:
    return {
        "number": invoice.number,
        "lines": len(invoice.items),
        "total": total_of(invoice),
        "paid": invoice.paid,
    }


def top_items(invoice: Invoice, limit: int = 3) -> list[Item]:
    ranked = sorted(invoice.items, key=lambda item: item.total(), reverse=True)
    return ranked[:limit]


def format_line(item: Item, width: int = 12) -> str:
    return f"{truncate(item.sku, width):<{width}} {item.quantity:>3} {item.total():>8.2f}"


def render(invoice: Invoice) -> str:
    lines = [f"Invoice {invoice.number}"]
    lines.extend(format_line(item) for item in invoice.items)
    lines.append(f"Total {total_of(invoice):.2f}")
    return "\n".join(lines)


def totals_by_currency(invoices: list[Invoice]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for invoice in invoices:
        totals[invoice.currency] = totals.get(invoice.currency, 0.0) + invoice.subtotal()
    return totals


def aging(invoices: list[Invoice]) -> tuple[int, int]:
    paid = sum(1 for item in invoices if item.paid)
    return paid, len(invoices) - paid
