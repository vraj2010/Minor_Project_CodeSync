from datetime import date, timedelta


def due_date(issued: date, days: int = 30) -> date:
    return issued + timedelta(days=days)


def is_overdue(due: date, today: date | None = None) -> bool:
    today = today or date.today()
    return due < today


def days_between(first: date, second: date) -> int:
    return abs((second - first).days)


def quarter_of(day: date) -> int:
    return (day.month - 1) // 3 + 1
