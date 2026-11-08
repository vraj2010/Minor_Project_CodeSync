import asyncio

from .errors import NotFoundError, ValidationError
from .models import Invoice, RecurringInvoice
from .parsing import iter_rows, parse_amount, parse_flag, parse_lines
from .pricing import apply_discount, bulk_price, price_band, split_evenly
from .rates import convert_all, load_rates
from .report import aging, summarize
from .store import AuditedRepository, Repository
from .text import truncate


def import_invoice(number: str, text: str) -> Invoice:
    invoice = Invoice(number)
    for item in parse_lines(text):
        invoice.add(item)
    return invoice


def first_row(lines):
    return next(iter_rows(lines))


def chained_invoice(number, first, second):
    return Invoice(number).add(first).add(second)


def discounted(invoice: Invoice, percent: float) -> Invoice:
    result = Invoice(invoice.number, currency=invoice.currency)
    for item in invoice.items:
        result.add(apply_discount(item, percent=percent))
    return result


def safe_amount(text: str, fallback: float = 0.0) -> float:
    try:
        return parse_amount(text)
    except ValidationError:
        return fallback


def pay(repository: Repository, number: str) -> bool:
    try:
        invoice = repository.get(number)
    except NotFoundError:
        return False
    invoice.paid = True
    return True


def lookup_or_new(repository: Repository, number: str) -> Invoice:
    return repository.find(number, Invoice(number))


def remove_if_present(repository: Repository, number: str) -> str:
    return "removed" if repository.delete(number) is True else "missing"


def describe(invoice: Invoice) -> str:
    summary = summarize(invoice)
    state = "paid" if summary["paid"] else "open"
    return f"{summary['number']}: {summary['total']:.2f} ({state})"


def band_label(amount: float) -> str:
    return price_band(amount).upper()


def bulk_summary(price: float, quantity: int) -> str:
    unit, total = bulk_price(price, quantity)
    return f"{quantity} x {unit:.2f} = {total:.2f}"


def paid_ratio(invoices) -> float:
    paid, unpaid = aging(invoices)
    return paid / (paid + unpaid)


def shares(amount: float, parts: int) -> int:
    return len(split_evenly(amount, parts))


def enabled(text: str) -> str:
    return "on" if parse_flag(text) is True else "off"


def converted_total(amounts, currency) -> float:
    return sum(asyncio.run(convert_all(amounts, currency, load_rates())))


def rate_table() -> list[str]:
    return sorted(load_rates().keys())


def short_title(title: str) -> str:
    return truncate(title, 10, "..")


def billed_amounts(invoices, rate: float) -> list[float]:
    return [invoice.total_with(rate) for invoice in invoices]


def audited() -> AuditedRepository:
    return AuditedRepository()


def recurring(number: str, cycles: int) -> RecurringInvoice:
    return RecurringInvoice(number, cycles=cycles)
