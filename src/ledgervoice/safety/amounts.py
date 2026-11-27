"""Turn the amount the model passes to propose_transfer into whole dollars (D16, D21, D53).

The model converts the user's words ("fifty bucks", "twelve hundred") into digits; this
module is the boundary that checks them. It parses with Decimal (never float) and
accepts only plain ASCII forms: "50", "$50", "$ 50", "1,200", "50.00" (at most one
space after "$", at most 20 characters, R2-09/R2-10). Anything else is refused with a
message the model can relay; nothing that isn't text is converted (R2-11).

A non-whole amount is not rounded: Nessie would silently truncate it (D16), so the
user is asked to choose. The suggestions are always valid amounts, and the message is
never a yes/no question, so a "yes" to it can't be mistaken for a choice (R2-02, R2-12).
"""

from __future__ import annotations

import re
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

MAX_TRANSFER = 2_000  # D29: per-transfer limit for the demo
MAX_TEXT = 20  # longer than "$2,000.00"; anything longer is refused before parsing

_AMOUNT = re.compile(r"\$? ?([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.([0-9]+))?", re.ASCII)


class AmountRejected(ValueError):
    """The amount can't be used; `str(exc)` is a short explanation for the user."""


def parse_amount(text: object) -> int:
    """Whole dollars from text like "50", "$1,200" or "50.00"; AmountRejected otherwise."""
    if not isinstance(text, str):
        raise AmountRejected("the amount must be given as text, like 50")
    cleaned = text.strip(" ")
    match = _AMOUNT.fullmatch(cleaned) if len(cleaned) <= MAX_TEXT else None
    if match is None:
        shown = text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "..."
        raise AmountRejected(
            f"I couldn't read {shown!r} as an amount of money; please say it in dollars"
        )
    value = Decimal(match.group(1).replace(",", "") + "." + (match.group(2) or "0"))
    if value < 1:
        raise AmountRejected("the smallest amount I can move is $1")
    if value > MAX_TRANSFER:
        raise AmountRejected(f"the most I can move in one transfer is ${MAX_TRANSFER:,}")
    if value != value.to_integral_value():
        # 1 < value < MAX_TRANSFER here, so both neighbours are valid amounts
        low = int(value.to_integral_value(rounding=ROUND_FLOOR))
        high = int(value.to_integral_value(rounding=ROUND_CEILING))
        raise AmountRejected(
            "transfers are in whole dollars; please choose a whole-dollar amount, "
            f"such as ${low:,} or ${high:,}"
        )
    return int(value)
