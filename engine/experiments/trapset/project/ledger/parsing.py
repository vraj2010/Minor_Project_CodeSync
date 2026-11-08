from .errors import ValidationError
from .models import Item


def parse_amount(text: str) -> float:
    cleaned = text.strip().lstrip("$").replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        raise ValidationError(f"not an amount: {text!r}") from None


def parse_flag(text: str) -> bool:
    return text.strip().lower() in ("1", "true", "yes", "y")


def parse_row(line: str, sep: str = ",", strict: bool = False) -> Item:
    parts = [part.strip() for part in line.split(sep)]
    if strict and len(parts) != 3:
        raise ValidationError(f"expected 3 fields: {line!r}")
    if len(parts) < 2:
        raise ValidationError(f"too few fields: {line!r}")
    quantity = int(parts[2]) if len(parts) > 2 else 1
    return Item(parts[0], parse_amount(parts[1]), quantity)


def parse_lines(text: str, sep: str = ",") -> list[Item]:
    items = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        items.append(parse_row(stripped, sep=sep))
    return items


def iter_rows(lines, sep=","):
    for line in lines:
        yield parse_row(line, sep)
