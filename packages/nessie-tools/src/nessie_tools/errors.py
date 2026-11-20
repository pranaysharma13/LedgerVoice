"""Error types for nessie-tools.

Two families, so callers can tell them apart (R1-21):
- BankError:   something went wrong talking to the bank, or a value the bank can't
               store (AmountError). The subclasses tell the saga whether a failed
               write definitely changed nothing, or may have.
- LedgerError: our own rules said no: a transfer was rejected (TransferRejected) or
               an audit-log operation was invalid (AuditError). Defined in
               transfers.py and audit.py.
"""

from __future__ import annotations


class BankError(Exception):
    """Base class for bank and transport errors."""


class LedgerError(Exception):
    """Base class for errors raised by our own rules (rejections, audit misuse)."""


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
    - A Nessie error with no HTTP status (timeout, dropped connection, unreadable
      response) or a 5xx may have been processed by the server before the error.
    - A 2xx means the server ACCEPTED the write; the error came afterwards (e.g. a
      body we couldn't parse), so it was very likely written (R1-03).
    - A 4xx means the server rejected the request: nothing was written.
    """
    if isinstance(exc, BackendFailure):
        return exc.ambiguous
    if isinstance(exc, NessieError):
        code = exc.status_code
        return code is None or code >= 500 or 200 <= code < 300
    return False
