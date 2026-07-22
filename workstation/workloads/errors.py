"""Public-safe workload errors; raw provider details stay in local journald."""


class WorkloadError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = str(code)[:80]
        self.retryable = bool(retryable)
