"""Turn the amount the model passes to propose_transfer into whole dollars (D16, D21, D53).

The model converts the user's words ("fifty bucks", "twelve hundred") into digits; this
module is the boundary that checks them. It parses with Decimal (never float), accepts
"50", "$50", "1,200", "50.00", and refuses anything else with a message the model can
relay. A non-whole amount is not rounded: Nessie would silently truncate it (D16), so
the user is asked to choose.
"""

from __future__ import annotations

import re
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

MAX_TRANSFER = 2_000  # D29: per-transfer limit for the demo

_AMOUNT = re.compile(r"^\$?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?$")


class AmountRejected(ValueError):
    """The amount can't be used; `str(exc)` is a short explanation for the user."""


def parse_amount(text: str) -> int:
    """Whole dollars from text like "50", "$1,200" or "50.00"; AmountRejected otherwise."""
    cleaned = text.strip()
    match = _AMOUNT.fullmatch(cleaned)
    if match is None:
        raise AmountRejected(
            f"I couldn't read {text!r} as an amount of money; please say it in dollars"
        )
    value = Decimal(match.group(1).replace(",", "") + "." + (match.group(2) or "0"))
    if value != value.to_integral_value():
        low = int(value.to_integral_value(rounding=ROUND_FLOOR))
        high = int(value.to_integral_value(rounding=ROUND_CEILING))
        options = f"${high}" if low == 0 else f"${low} or ${high}"
        raise AmountRejected(
            f"transfers are in whole dollars; would you like to send {options} instead?"
        )
    dollars = int(value)
    if dollars <= 0:
        raise AmountRejected("the amount has to be more than zero")
    if dollars > MAX_TRANSFER:
        raise AmountRejected(f"the most I can move in one transfer is ${MAX_TRANSFER:,}")
    return dollars
