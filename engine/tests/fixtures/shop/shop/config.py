TAX = 0.18
LIMITS = {"max": 10}
counter = 0


def bump():
    global counter
    counter += 1
    return counter
