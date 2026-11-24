"""Transfer policy: which proposals may even be read back to the user (D29, D53).

Checked here, in code, before anything is recorded:
- accounts are named by their last 4 digits and must be one of THIS customer's
  accounts (the model never sees or sends bank ids);
- source and destination differ;
- money can be moved into a credit card (a card payment) but never out of one;
- the amount parses as whole dollars, more than zero, at most $2,000 (amounts.py).
Funds and the accounts' existence are checked again by TransferService.create_pending
and once more at execution (D34).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from nessie_tools import Account, AccountType

from ledgervoice.safety.amounts import AmountRejected, parse_amount

_LAST4 = re.compile(r"\d{4}")

ACCOUNT_WORDS = {
    AccountType.CHECKING: "checking",
    AccountType.SAVINGS: "savings",
    AccountType.CREDIT_CARD: "credit card",
}


class ProposalRejected(ValueError):
    """Not allowed; `str(exc)` is a short explanation the model can relay."""


@dataclass(frozen=True)
class Proposal:
    source: Account
    destination: Account
    amount: int

    def read_back(self) -> str:
        """The exact confirmation question, written by code, never by the model (D26)."""
        return (
            f"You're moving ${self.amount:,} from {describe(self.source)} to "
            f"{describe(self.destination)}. Say yes to confirm, or anything else to cancel."
        )


def describe(account: Account) -> str:
    return f"{ACCOUNT_WORDS.get(account.type, 'account')} ending in {account.last4}"


def check_proposal(
    accounts: list[Account], from_last4: str, to_last4: str, amount_text: str
) -> Proposal:
    source = _find(accounts, from_last4, "from")
    destination = _find(accounts, to_last4, "to")
    if source.id == destination.id:
        raise ProposalRejected("the two accounts are the same; pick two different accounts")
    if source.type is AccountType.CREDIT_CARD:
        raise ProposalRejected(
            "money can't be moved out of a credit card; you can pay into it from "
            "checking or savings"
        )
    try:
        amount = parse_amount(amount_text)
    except AmountRejected as exc:
        raise ProposalRejected(str(exc)) from None
    return Proposal(source, destination, amount)


def _find(accounts: list[Account], last4: str, which: str) -> Account:
    ref = last4.strip()
    if not _LAST4.fullmatch(ref):
        raise ProposalRejected(f"the {which} account must be given as its last 4 digits, like 1234")
    matches = [a for a in accounts if a.last4 == ref]
    if not matches:
        raise ProposalRejected(f"you don't have an account ending in {ref}")
    if len(matches) > 1:
        raise ProposalRejected(f"more than one of your accounts ends in {ref}")
    return matches[0]
