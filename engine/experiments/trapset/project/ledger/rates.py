RATES = {"USD": 1.0, "EUR": 0.5, "INR": 80.0}


def load_rates() -> dict[str, float]:
    return dict(RATES)


async def fetch_rate(currency: str, rates: dict[str, float]) -> float:
    return rates[currency]


async def convert_all(amounts: list[float], currency: str, rates: dict[str, float]) -> list[float]:
    rate = await fetch_rate(currency, rates)
    return [amount * rate for amount in amounts]
