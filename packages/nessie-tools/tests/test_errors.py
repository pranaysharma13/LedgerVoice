import pytest
from nessie_tools import (
    AmountError,
    BackendFailure,
    BankError,
    LedgerError,
    NessieError,
    NotFoundError,
    TransferRejected,
    is_ambiguous,
)


@pytest.mark.parametrize(
    ("exc", "ambiguous"),
    [
        (NessieError("timeout"), True),  # no response at all
        (NessieError("server error", status_code=503), True),
        (NessieError("accepted, unreadable body", status_code=201), True),  # R1-03
        (NessieError("validation", status_code=400), False),  # rejected: nothing written
        (NessieError("not found", status_code=404), False),
        (BackendFailure("x", ambiguous=True), True),
        (BackendFailure("x"), False),
        (NotFoundError("x"), False),
        (AmountError("x"), False),
    ],
)
def test_is_ambiguous(exc, ambiguous):
    assert is_ambiguous(exc) is ambiguous


def test_rejections_are_not_bank_errors():
    """R1-21: 'our rules said no' is a different thing from 'the bank failed'."""
    assert issubclass(TransferRejected, LedgerError)
    assert not issubclass(TransferRejected, BankError)
