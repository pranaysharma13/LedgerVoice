"""Error types for nessie-tools.

Every error raised by this package is a BankError, so callers can catch one type.
The subclasses tell the caller what kind of failure it was, which matters for the
transfer saga: some failures definitely changed nothing, others may have.
"""

from __future__ import annotations


class BankError(Exception):
    """Base class for every error raised by nessie-tools."""


class NessieError(BankError):
    """HTTP or transport error from the Nessie API. The message never contains the key."""

    def __init__(self, message: str, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class NotFoundError(BankError):
    """The requested customer, account or merchant does not exist."""


class AmountError(BankError, ValueError):
    """An amount is not a positive whole number of dollars (decision D16)."""


class BackendFailure(BankError):
    """A failure injected by FakeBackend in tests.

    ambiguous=True simulates a timeout AFTER the write happened: the caller cannot
    tell whether the operation took effect. That is the case the saga must handle
    without creating a duplicate.
    """

    def __init__(self, message: str, *, ambiguous: bool = False):
        super().__init__(message)
        self.ambiguous = ambiguous


def is_ambiguous(exc: BaseException) -> bool:
    """True if a failed write MAY have taken effect, so it must be checked, not retried.

    - Injected failures say so explicitly.
    - A Nessie error with no HTTP status (timeout, dropped connection) or a 5xx may
      have been processed by the server before the error.
    - A 4xx means the server rejected the request: nothing was written.
    """
    if isinstance(exc, BackendFailure):
        return exc.ambiguous
    if isinstance(exc, NessieError):
        return exc.status_code is None or exc.status_code >= 500
    return False
