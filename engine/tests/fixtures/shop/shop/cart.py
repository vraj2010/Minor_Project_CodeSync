from shop import config
from shop.config import LIMITS, TAX


class Base:
    discount = 0.0

    def __init__(self, owner):
        self.owner = owner
        self.items = []


class Cart(Base):
    currency: str = "INR"

    def __init__(self, owner, tags=None, *extra, strict=False, **options):
        super().__init__(owner)
        self.total = 0
        self.items = []
        self.discount = 0.1

    def add(self, item, qty=1):
        subtotal = item.price * qty
        self.total += subtotal * (1 + TAX)
        scale = LIMITS["max"]
        names = [name.upper() for name in self.items if name]
        outer = 1

        def inner():
            nonlocal outer
            outer += 1
            return outer + scale

        return inner() + config.counter + Cart.currency.count("I") + len(names)

    @classmethod
    def build(cls, owner):
        cls.created = True
        return cls(owner)
