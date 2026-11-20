import pytest
from nessie_tools import (
    AmountError,
    BackendFailure,
    BankError,
    LedgerError,
    NessieDataError,
    NessieError,
    NotFoundError,
    TransferRejected,
    is_ambiguous,
    is_temporary,
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


@pytest.mark.parametrize(
    ("exc", "ambiguous", "temporary"),
    [
        # D41: "might a write have happened?" and "will retrying help?" differ.
        (NessieError("timeout"), True, True),
        (NessieError("server error", status_code=503), True, True),
        (NessieError("rate limited", status_code=429), False, True),
        (NessieError("accepted, unreadable body", status_code=201), True, False),
        (NessieDataError("bad record"), True, False),  # write probably landed; retry is futile
        (NessieDataError("non-JSON", status_code=200), True, False),
        (NessieError("validation", status_code=400), False, False),
        (NessieError("bad key", status_code=401), False, False),
        (NessieError("forbidden", status_code=403), False, False),
        (NessieError("not found", status_code=404), False, False),
        (BackendFailure("x", ambiguous=True), True, True),
        (BackendFailure("x"), False, False),
        (NotFoundError("x"), False, False),
        (AmountError("x"), False, False),
    ],
)
def test_ambiguous_and_temporary_are_separate_questions(exc, ambiguous, temporary):
    assert is_ambiguous(exc) is ambiguous
    assert is_temporary(exc) is temporary


def test_data_errors_are_nessie_errors():
    """Callers that catch NessieError keep working; the subclass only adds information."""
    assert issubclass(NessieDataError, NessieError)
