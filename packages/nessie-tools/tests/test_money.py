import pytest
from nessie_tools import AmountError, require_whole_dollars


@pytest.mark.parametrize("amount", [1, 50, 2000])
def test_positive_ints_pass(amount):
    assert require_whole_dollars(amount) == amount


@pytest.mark.parametrize(
    "amount",
    [
        50.5,  # cents: Nessie would silently store 50
        50.0,  # whole-valued float: still rejected, floats must never slip through
        True,  # bool is an int subclass in Python
        "50",  # strings are parsed elsewhere, never here
        None,
        0,
        -5,
    ],
)
def test_everything_else_is_rejected(amount):
    with pytest.raises(AmountError):
        require_whole_dollars(amount)


def test_zero_allowed_only_when_asked():
    assert require_whole_dollars(0, allow_zero=True) == 0
    with pytest.raises(AmountError):
        require_whole_dollars(-1, allow_zero=True)


def test_amount_error_is_also_a_value_error():
    # lets generic `except ValueError` handlers catch it too
    assert issubclass(AmountError, ValueError)
