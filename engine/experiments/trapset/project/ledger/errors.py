class LedgerError(Exception):
    pass


class ValidationError(LedgerError):
    pass


class NotFoundError(LedgerError):
    def __init__(self, key):
        super().__init__(f"no record for {key!r}")
        self.key = key
