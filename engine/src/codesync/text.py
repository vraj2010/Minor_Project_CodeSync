"""Small helpers for wording what the tools print."""


def plural(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")
