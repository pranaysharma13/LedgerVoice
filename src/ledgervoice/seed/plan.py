"""The seed plan: everything the bank should contain, decided before anything is written.

A plan is plain data (frozen dataclasses). It is produced by `generate()` without any
network access, written to the bank by the applier (M1c-2), and is the only input to
the ground truth (truth.py). Every item has a stable `key` such as "priya/checking" or
"priya/tx/0042"; the applier's manifest maps these keys to the ids the bank assigns.

`to_json()` is canonical (sorted keys, fixed separators, no floats), so the same plan
always produces the same bytes and `digest()` can pin it in a golden test.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from nessie_tools import AccountType, BillStatus, TxStatus


class TxKind(StrEnum):
    PURCHASE = "purchase"
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"


@dataclass(frozen=True)
class PlanMerchant:
    key: str
    name: str
    category: str


@dataclass(frozen=True)
class PlanCustomer:
    key: str
    first_name: str
    last_name: str
    persona: str  # "student" | "professional" | "traveller"


@dataclass(frozen=True)
class PlanAccount:
    key: str
    customer_key: str
    type: AccountType
    nickname: str
    opening_balance: int  # Nessie's stored balance = balance at the start of the window (D18)


@dataclass(frozen=True)
class PlanTx:
    key: str
    account_key: str
    kind: TxKind
    amount: int
    day: date
    description: str
    status: TxStatus
    merchant_key: str | None = None  # purchases only
    internal: bool = False  # one leg of a move between the customer's own accounts


@dataclass(frozen=True)
class PlanBill:
    key: str
    account_key: str
    payee: str
    nickname: str
    amount: int
    payment_date: date
    recurring_day: int  # always set: Nessie breaks bill listing without it (D49)
    status: BillStatus
    paid_by_tx_key: str | None = None  # a completed bill names the purchase that paid it


@dataclass(frozen=True)
class SeedPlan:
    seed: int
    anchor: date  # "today" for this data set (D23); history ends the day before
    start: date  # first day of history
    merchants: tuple[PlanMerchant, ...]
    customers: tuple[PlanCustomer, ...]
    accounts: tuple[PlanAccount, ...]
    transactions: tuple[PlanTx, ...]
    bills: tuple[PlanBill, ...]

    # --- lookups -----------------------------------------------------------------
    def merchant(self, key: str) -> PlanMerchant:
        return next(m for m in self.merchants if m.key == key)

    def account(self, key: str) -> PlanAccount:
        return next(a for a in self.accounts if a.key == key)

    def accounts_of(self, customer_key: str) -> list[PlanAccount]:
        return [a for a in self.accounts if a.customer_key == customer_key]

    def subset(self, customer_key: str, max_transactions: int) -> SeedPlan:
        """A small, self-consistent plan: one customer, their accounts, their first
        `max_transactions` transactions, the merchants those use, and the bills whose
        paying purchase is kept (or that move no money). Used by the live seed test."""
        accounts = tuple(a for a in self.accounts if a.customer_key == customer_key)
        keys = {a.key for a in accounts}
        txs = tuple(t for t in self.transactions if t.account_key in keys)[:max_transactions]
        kept = {t.key for t in txs}
        bills = tuple(
            b
            for b in self.bills
            if b.account_key in keys and (b.paid_by_tx_key is None or b.paid_by_tx_key in kept)
        )
        used = {t.merchant_key for t in txs if t.merchant_key is not None}
        return SeedPlan(
            seed=self.seed,
            anchor=self.anchor,
            start=self.start,
            merchants=tuple(m for m in self.merchants if m.key in used),
            customers=tuple(c for c in self.customers if c.key == customer_key),
            accounts=accounts,
            transactions=txs,
            bills=bills,
        )

    # --- canonical form ----------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        plain: dict[str, Any] = _plain(asdict(self))
        return plain

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()


def _plain(value: Any) -> Any:
    """Dates → ISO strings, enums → their values, tuples → lists (JSON-ready)."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        raise TypeError("plans hold whole dollars only (D16); found a float")
    return value
