from inventory.models import *


def restock(stock, sku, quantity):
    stock.add(Item(sku, 0.0, quantity))
    return stock.value()
