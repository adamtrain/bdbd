"""Domain errors carry a stable machine-readable code for the JSON envelope."""


class CashError(Exception):
    code = "error"

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        self.message = message
