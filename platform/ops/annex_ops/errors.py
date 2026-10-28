"""One error type for everything a caller can fix: an HTTP status and a message that says how."""


class OpsError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)
