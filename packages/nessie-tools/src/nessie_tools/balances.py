"""Computed balances (decision D18).

Nessie never updates an account's stored `balance` when transactions are added, so
the stored value is treated as the OPENING balance (set at seed time) and the current
balance is computed from the transaction records:

    current = opening + completed deposits - completed withdrawals - completed purchases

Only `completed` items count. Pending, cancelled, or unknown-status items don't move
money yet. For a credit card the same formula applies; a negative result means the
amount owed.
"""

from __future__ import annotations

from dataclasses import dataclass

from nessie_tools.backend import BankBackend
from nessie_tools.models import Deposit, Purchase, TxStatus, Withdrawal


@dataclass(frozen=True)
class Balance:
    account_id: str
    opening: int
    deposits: int
    withdrawals: int
    purchases: int

    @property
    def current(self) -> int:
        return self.opening + self.deposits - self.withdrawals - self.purchases


def _completed_total(items: list[Deposit] | list[Withdrawal] | list[Purchase]) -> int:
    return sum(item.amount for item in items if item.status is TxStatus.COMPLETED)


def compute_balance(backend: BankBackend, account_id: str) -> Balance:
    account = backend.get_account(account_id)
    return Balance(
        account_id=account_id,
        opening=account.balance,
        deposits=_completed_total(backend.list_deposits(account_id)),
        withdrawals=_completed_total(backend.list_withdrawals(account_id)),
        purchases=_completed_total(backend.list_purchases(account_id)),
    )
