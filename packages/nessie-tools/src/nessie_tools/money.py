"""The single gate for amounts (decision D16).

Nessie stores whole dollars and silently truncates anything else ($4.75 becomes $4),
so every amount is checked here before it can reach any backend.
"""

from __future__ import annotations

from typing import Any

from nessie_tools.errors import AmountError


def require_whole_dollars(amount: Any, *, allow_zero: bool = False) -> int:
    """Return `amount` as an int, or raise AmountError.

    Accepted: a plain int (not bool) that is > 0, or >= 0 when allow_zero=True
    (opening balances may be zero). Everything else is rejected, including floats
    that happen to be whole (50.0): callers must convert deliberately, so a float
    can never slip through from user input or arithmetic.
    """
    # bool is a subclass of int in Python: True would otherwise pass as 1.
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise AmountError(
            f"amount must be a whole number of dollars (int), got {type(amount).__name__} "
            f"{amount!r}"
        )
    minimum = 0 if allow_zero else 1
    if amount < minimum:
        raise AmountError(f"amount must be >= {minimum}, got {amount}")
    return amount
