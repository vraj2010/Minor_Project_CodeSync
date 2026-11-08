"""Fifty changes that each look reasonable and each hide exactly one break, and
twelve controls that are harmless.

Every trap is proved real by the hidden suite in `oracle/`: it passes on the
original project and fails with the trap applied. Every control has to leave it
passing. The checks never see that suite, so a trap the project's own tests
cannot reach stays a real test of what the checks can do. Some of these are
expected to get through: that is the point of measuring.
"""


def trap(id, category, target, title, rewrite):
    return {
        "id": id,
        "category": category,
        "target": target,
        "title": title,
        "rewrite": rewrite,
    }


TRAPS = [
    # --- the way a function is called ---------------------------------------
    trap(
        "T01",
        "signature",
        "ledger.parsing.parse_row",
        "A clearer parameter name, made keyword-only",
        '''
def parse_row(line: str, *, delimiter: str = ",", strict: bool = False) -> Item:
    parts = [part.strip() for part in line.split(delimiter)]
    if strict and len(parts) != 3:
        raise ValidationError(f"expected 3 fields: {line!r}")
    if len(parts) < 2:
        raise ValidationError(f"too few fields: {line!r}")
    quantity = int(parts[2]) if len(parts) > 2 else 1
    return Item(parts[0], parse_amount(parts[1]), quantity)
''',
    ),
    trap(
        "T02",
        "signature",
        "ledger.parsing.parse_lines",
        "The separator is now always explicit",
        '''
def parse_lines(text: str, sep: str) -> list[Item]:
    items = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        items.append(parse_row(stripped, sep=sep))
    return items
''',
    ),
    trap(
        "T03",
        "signature",
        "ledger.pricing.tax_for",
        "The rate comes first, because it rarely changes",
        '''
def tax_for(rate: float = TAX_RATE, amount: float = 0.0) -> float:
    return round_money(amount * rate)
''',
    ),
    trap(
        "T04",
        "signature",
        "ledger.pricing.apply_discount",
        "A more descriptive parameter name",
        '''
def apply_discount(item: Item, discount_percent: float) -> Item:
    if not 0 <= discount_percent <= 100:
        raise ValidationError(f"bad percent {discount_percent}")
    price = round_money(item.price * (100 - discount_percent) / 100)
    return Item(item.sku, price, item.quantity)
''',
    ),
    trap(
        "T05",
        "signature",
        "ledger.text.truncate",
        "The suffix is keyword-only, to make calls readable",
        '''
def truncate(text: str, limit: int, *, suffix: str = "...") -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(suffix)] + suffix
''',
    ),
    trap(
        "T06",
        "signature",
        "ledger.store.Repository.find",
        "A simpler lookup without the rarely used default",
        '''
def find(self, number: str):
    return self._invoices.get(number)
''',
    ),
    trap(
        "T07",
        "signature",
        "ledger.models.Invoice.add",
        "Adding an item now states its quantity",
        '''
def add(self, item: Item, quantity: int) -> "Invoice":
    self.items.append(item.with_quantity(quantity))
    return self
''',
    ),
    trap(
        "T08",
        "signature",
        "ledger.report.top_items",
        "A more useful default length",
        '''
def top_items(invoice: Invoice, limit: int = 5) -> list[Item]:
    ranked = sorted(invoice.items, key=lambda item: item.total(), reverse=True)
    return ranked[:limit]
''',
    ),
    trap(
        "T09",
        "signature",
        "ledger.dates.due_date",
        "A shorter default payment term",
        '''
def due_date(issued: date, days: int = 14) -> date:
    return issued + timedelta(days=days)
''',
    ),
    trap(
        "T10",
        "signature",
        "ledger.models.RecurringInvoice.total_with",
        "Recurring invoices do not need a rate here",
        '''
def total_with(self) -> float:
    return super().total_with(0.0) + 1.0
''',
    ),
    # --- what comes back -----------------------------------------------------
    trap(
        "T11",
        "return value",
        "ledger.models.Invoice.add",
        "A mutating method should not return anything",
        '''
def add(self, item: Item) -> None:
    self.items.append(item)
''',
    ),
    trap(
        "T12",
        "return value",
        "ledger.parsing.parse_row",
        "A plain tuple is lighter than an object",
        '''
def parse_row(line: str, sep: str = ",", strict: bool = False):
    parts = [part.strip() for part in line.split(sep)]
    if strict and len(parts) != 3:
        raise ValidationError(f"expected 3 fields: {line!r}")
    if len(parts) < 2:
        raise ValidationError(f"too few fields: {line!r}")
    quantity = int(parts[2]) if len(parts) > 2 else 1
    return (parts[0], parse_amount(parts[1]), quantity)
''',
    ),
    trap(
        "T13",
        "return value",
        "ledger.pricing.bulk_price",
        "Also report whether the bulk rate applied",
        '''
def bulk_price(price: float, quantity: int):
    discounted = quantity >= 10
    unit = price * 0.9 if discounted else price
    return unit, round_money(unit * quantity), discounted
''',
    ),
    trap(
        "T14",
        "return value",
        "ledger.report.summarize",
        "Payment state does not belong in a summary",
        '''
def summarize(invoice: Invoice) -> dict:
    return {
        "number": invoice.number,
        "lines": len(invoice.items),
        "total": total_of(invoice),
    }
''',
    ),
    trap(
        "T15",
        "return value",
        "ledger.store.Repository.delete",
        "Deleting needs no return value",
        '''
def delete(self, number: str) -> None:
    self._invoices.pop(number, None)
''',
    ),
    trap(
        "T16",
        "return value",
        "ledger.pricing.price_band",
        "Numeric bands sort better than strings",
        '''
def price_band(amount: float) -> int:
    if amount < 100:
        return 1
    if amount < 1000:
        return 2
    return 3
''',
    ),
    trap(
        "T17",
        "return value",
        "ledger.report.aging",
        "A named result is clearer than a pair",
        '''
def aging(invoices: list[Invoice]) -> dict:
    paid = sum(1 for item in invoices if item.paid)
    return {"paid": paid, "unpaid": len(invoices) - paid}
''',
    ),
    trap(
        "T18",
        "return value",
        "ledger.pricing.split_evenly",
        "Yield the shares instead of building a list",
        '''
def split_evenly(amount: float, parts: int):
    if parts < 1:
        raise ValidationError("parts must be at least 1")
    cents = round(amount * 100)
    share, extra = divmod(cents, parts)
    for index in range(parts):
        yield (share + (1 if index < extra else 0)) / 100
''',
    ),
    trap(
        "T19",
        "return value",
        "ledger.dates.is_overdue",
        "An early return reads more simply",
        '''
def is_overdue(due: date, today: date | None = None) -> bool:
    today = today or date.today()
    if due < today:
        return True
''',
    ),
    trap(
        "T20",
        "return value",
        "ledger.parsing.parse_flag",
        "Hand back the normalised text, which is still truthy",
        '''
def parse_flag(text: str):
    value = text.strip().lower()
    return value if value in ("1", "true", "yes", "y") else ""
''',
    ),
    # --- how a call is made --------------------------------------------------
    trap(
        "T21",
        "async",
        "ledger.rates.fetch_rate",
        "It never waits for anything, so it need not be async",
        '''
def fetch_rate(currency: str, rates: dict[str, float]) -> float:
    return rates[currency]
''',
    ),
    trap(
        "T22",
        "async",
        "ledger.parsing.iter_rows",
        "A list comprehension is simpler than a generator",
        '''
def iter_rows(lines, sep=","):
    return [parse_row(line, sep) for line in lines]
''',
    ),
    trap(
        "T23",
        "async",
        "ledger.rates.convert_all",
        "Tidied, with one await lost on the way",
        '''
async def convert_all(amounts: list[float], currency: str, rates: dict[str, float]) -> list[float]:
    rate = fetch_rate(currency, rates)
    return [amount * rate for amount in amounts]
''',
    ),
    trap(
        "T24",
        "async",
        "ledger.rates.load_rates",
        "Ready for rates that come from a service",
        '''
async def load_rates() -> dict[str, float]:
    return dict(RATES)
''',
    ),
    # --- failures ------------------------------------------------------------
    trap(
        "T25",
        "exceptions",
        "ledger.parsing.parse_amount",
        "Let float report its own error",
        '''
def parse_amount(text: str) -> float:
    return float(text.strip().lstrip("$").replace(",", ""))
''',
    ),
    trap(
        "T26",
        "exceptions",
        "ledger.store.Repository.get",
        "A dict lookup with get is more direct",
        '''
def get(self, number: str) -> Invoice:
    return self._invoices.get(number)
''',
    ),
    trap(
        "T27",
        "exceptions",
        "ledger.pricing.apply_discount",
        "Clamp the percentage rather than fail",
        '''
def apply_discount(item: Item, percent: float) -> Item:
    percent = min(max(percent, 0), 100)
    price = round_money(item.price * (100 - percent) / 100)
    return Item(item.sku, price, item.quantity)
''',
    ),
    trap(
        "T28",
        "exceptions",
        "ledger.models.Invoice.remove",
        "A generator expression replaces the helper",
        '''
def remove(self, sku: str) -> Item:
    item = next(line for line in self.items if line.sku == sku)
    self.items.remove(item)
    return item
''',
    ),
    trap(
        "T29",
        "exceptions",
        "ledger.parsing.parse_lines",
        "One bad line should not stop an import",
        '''
def parse_lines(text: str, sep: str = ",") -> list[Item]:
    items = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            items.append(parse_row(stripped, sep=sep))
        except ValidationError:
            continue
    return items
''',
    ),
    trap(
        "T30",
        "exceptions",
        "ledger.app.pay",
        "Catch the base class, in case more errors are added",
        '''
from .errors import LedgerError


def pay(repository: Repository, number: str) -> bool:
    try:
        invoice = repository.get(number)
    except LedgerError:
        return False
    invoice.paid = True
    return True
''',
    ),
    # --- names that are not there --------------------------------------------
    trap(
        "T31",
        "names",
        "ledger.pricing.round_money",
        "Decimal rounds money correctly",
        '''
def round_money(amount: float) -> float:
    return float(Decimal(str(amount)).quantize(Decimal("0.01")))
''',
    ),
    trap(
        "T32",
        "names",
        "ledger.models.Invoice.subtotal",
        "A loop over the invoice lines",
        '''
def subtotal(self) -> float:
    return sum(line.total() for line in self.lines)
''',
    ),
    trap(
        "T33",
        "names",
        "ledger.report.format_line",
        "The standard library already shortens text",
        '''
def format_line(item: Item, width: int = 12) -> str:
    name = textwrap.shorten(item.sku, width)
    return f"{name:<{width}} {item.quantity:>3} {item.total():>8.2f}"
''',
    ),
    trap(
        "T34",
        "names",
        "ledger.report.render",
        "A helper with a better name",
        '''
def render(invoice: Invoice) -> str:
    lines = [f"Invoice {invoice.number}"]
    lines.extend(format_item(item) for item in invoice.items)
    lines.append(f"Total {total_of(invoice):.2f}")
    return "\\n".join(lines)
''',
    ),
    trap(
        "T35",
        "names",
        "ledger.report.summarize",
        "Import the total under its own name",
        '''
from .pricing import total_for


def summarize(invoice: Invoice) -> dict:
    return {
        "number": invoice.number,
        "lines": len(invoice.items),
        "total": total_for(invoice),
        "paid": invoice.paid,
    }
''',
    ),
    trap(
        "T36",
        "names",
        "ledger.pricing.invoice_total",
        "Passing the rate by name, for clarity",
        '''
def invoice_total(invoice: Invoice, rate: float = TAX_RATE) -> float:
    subtotal = invoice.subtotal()
    return round_money(subtotal + tax_for(subtotal, tax=rate))
''',
    ),
    # --- behaviour -----------------------------------------------------------
    trap(
        "T37",
        "behaviour",
        "ledger.pricing.round_money",
        "The small constant looks like a mistake",
        '''
def round_money(amount: float) -> float:
    return round(amount, 2)
''',
    ),
    trap(
        "T38",
        "behaviour",
        "ledger.pricing.split_evenly",
        "Each person simply pays an equal share",
        '''
def split_evenly(amount: float, parts: int) -> list[float]:
    if parts < 1:
        raise ValidationError("parts must be at least 1")
    return [round(amount / parts, 2)] * parts
''',
    ),
    trap(
        "T39",
        "behaviour",
        "ledger.pricing.price_band",
        "Inclusive limits read more naturally",
        '''
def price_band(amount: float) -> str:
    if amount <= 100:
        return "low"
    if amount <= 1000:
        return "mid"
    return "high"
''',
    ),
    trap(
        "T40",
        "behaviour",
        "ledger.report.top_items",
        "Sort in place and save a copy",
        '''
def top_items(invoice: Invoice, limit: int = 3) -> list[Item]:
    invoice.items.sort(key=lambda item: item.total(), reverse=True)
    return invoice.items[:limit]
''',
    ),
    trap(
        "T41",
        "behaviour",
        "ledger.parsing.parse_lines",
        "A simpler filter for empty lines",
        '''
def parse_lines(text: str, sep: str = ",") -> list[Item]:
    items = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        items.append(parse_row(stripped, sep=sep))
    return items
''',
    ),
    trap(
        "T42",
        "behaviour",
        "ledger.text.truncate",
        "A simpler slice",
        '''
def truncate(text: str, limit: int, suffix: str = "...") -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + suffix
''',
    ),
    trap(
        "T43",
        "behaviour",
        "ledger.text.slugify",
        "A simpler pattern",
        '''
def slugify(text: str) -> str:
    lowered = text.strip().lower()
    return re.sub(r"[^a-z0-9]", "-", lowered).strip("-")
''',
    ),
    trap(
        "T44",
        "behaviour",
        "ledger.store.Repository.save",
        "Store a copy, to protect the repository from later changes",
        '''
from dataclasses import replace


def save(self, invoice: Invoice) -> Invoice:
    self._invoices[invoice.number] = replace(invoice)
    return invoice
''',
    ),
    trap(
        "T45",
        "behaviour",
        "ledger.store.Repository.numbers",
        "Dicts keep their order, so sorting is wasted",
        '''
def numbers(self) -> list[str]:
    return list(self._invoices)
''',
    ),
    trap(
        "T46",
        "behaviour",
        "ledger.models.Item.with_quantity",
        "Avoid building a new object",
        '''
def with_quantity(self, quantity: int) -> "Item":
    self.quantity = quantity
    return self
''',
    ),
    trap(
        "T47",
        "behaviour",
        "ledger.dates.quarter_of",
        "The minus one looked redundant",
        '''
def quarter_of(day: date) -> int:
    return day.month // 3 + 1
''',
    ),
    trap(
        "T48",
        "behaviour",
        "ledger.report.totals_by_currency",
        "A plain assignment is easier to read",
        '''
def totals_by_currency(invoices: list[Invoice]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for invoice in invoices:
        totals[invoice.currency] = invoice.subtotal()
    return totals
''',
    ),
    trap(
        "T49",
        "behaviour",
        "ledger.store.AuditedRepository.__init__",
        "A list default saves the None check",
        '''
def __init__(self, invoices=None, log=[]):
    self.log = log
    super().__init__(invoices)
''',
    ),
    trap(
        "T50",
        "behaviour",
        "ledger.dates.days_between",
        "The arguments are always in order, so abs is wasted",
        '''
def days_between(first: date, second: date) -> int:
    return (second - first).days
''',
    ),
]


CONTROLS = [
    trap(
        "C01",
        "control",
        "ledger.parsing.parse_lines",
        "A comprehension over cleaned lines",
        '''
def parse_lines(text: str, sep: str = ",") -> list[Item]:
    rows = (line.strip() for line in text.splitlines())
    return [parse_row(row, sep=sep) for row in rows if row and not row.startswith("#")]
''',
    ),
    trap(
        "C02",
        "control",
        "ledger.models.Invoice.subtotal",
        "A plain loop instead of a generator",
        '''
def subtotal(self) -> float:
    total = 0.0
    for item in self.items:
        total += item.total()
    return total
''',
    ),
    trap(
        "C03",
        "control",
        "ledger.pricing.tax_for",
        "A named intermediate value",
        '''
def tax_for(amount: float, rate: float = TAX_RATE) -> float:
    tax = amount * rate
    return round_money(tax)
''',
    ),
    trap(
        "C04",
        "control",
        "ledger.text.slugify",
        "Find the words rather than replace the rest",
        '''
def slugify(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words)
''',
    ),
    trap(
        "C05",
        "control",
        "ledger.store.Repository.numbers",
        "Name the keys explicitly",
        '''
def numbers(self) -> list[str]:
    return sorted(self._invoices.keys())
''',
    ),
    trap(
        "C06",
        "control",
        "ledger.pricing.price_band",
        "A single if and elif chain",
        '''
def price_band(amount: float) -> str:
    if amount < 100:
        band = "low"
    elif amount < 1000:
        band = "mid"
    else:
        band = "high"
    return band
''',
    ),
    trap(
        "C07",
        "control",
        "ledger.models.Invoice.count",
        "A plain loop",
        '''
def count(self) -> int:
    total = 0
    for item in self.items:
        total += item.quantity
    return total
''',
    ),
    trap(
        "C08",
        "control",
        "ledger.report.top_items",
        "A new optional parameter, leaving calls as they are",
        '''
def top_items(invoice: Invoice, limit: int = 3, reverse: bool = True) -> list[Item]:
    ranked = sorted(invoice.items, key=lambda item: item.total(), reverse=reverse)
    return ranked[:limit]
''',
    ),
    trap(
        "C09",
        "control",
        "ledger.pricing.apply_discount",
        "The validation stated more clearly",
        '''
def apply_discount(item: Item, percent: float) -> Item:
    if percent < 0 or percent > 100:
        raise ValidationError(f"percent must be between 0 and 100, got {percent}")
    price = round_money(item.price * (100 - percent) / 100)
    return Item(item.sku, price, item.quantity)
''',
    ),
    trap(
        "C10",
        "control",
        "ledger.text.initials",
        "Upper-case once at the end. No project test reaches this function",
        '''
def initials(name: str) -> str:
    return "".join(word[0] for word in name.split()).upper()
''',
    ),
    trap(
        "C11",
        "control",
        "ledger.models.Invoice.find",
        "A generator with a default",
        '''
def find(self, sku: str) -> Item | None:
    return next((item for item in self.items if item.sku == sku), None)
''',
    ),
    trap(
        "C12",
        "control",
        "ledger.report.render",
        "Build the lines in one expression",
        '''
def render(invoice: Invoice) -> str:
    body = [format_line(item) for item in invoice.items]
    lines = [f"Invoice {invoice.number}", *body, f"Total {total_of(invoice):.2f}"]
    return "\\n".join(lines)
''',
    ),
]
