"""Ground truth for a seed plan: the answer key for evals (D24, D28).

Everything here is computed from the plan alone and never reads a bank. Evals compare
the agent's answers, which come from the bank through nessie-tools, against these, so
the answer key is independent of the code being tested. A test writes the plan into
the fake bank and checks that compute_balance agrees with balance() for every account.

Conventions (D48):
- Balances count completed items only, like compute_balance (D18).
- A credit card is reported as amount owed = completed purchases - payments (D45).
- Spending = purchases (bill payments included, internal moves excluded). Completed
  only unless include_pending=True. Date ranges are inclusive.
- Income = deposits to checking that are not internal moves.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from nessie_tools import AccountType, BillStatus, TxStatus

from ledgervoice.seed.plan import PlanBill, PlanTx, SeedPlan, TxKind


@dataclass(frozen=True)
class Spend:
    total: int
    count: int


class GroundTruth:
    def __init__(self, plan: SeedPlan) -> None:
        self.plan = plan
        self._txs_by_account: dict[str, list[PlanTx]] = defaultdict(list)
        for tx in plan.transactions:
            self._txs_by_account[tx.account_key].append(tx)

    # --- windows ---------------------------------------------------------------
    @property
    def today(self) -> date:
        return self.plan.anchor

    def last_n_days(self, n: int) -> tuple[date, date]:
        """The n days before today: (today - n, today - 1), inclusive."""
        return self.today - timedelta(days=n), self.today - timedelta(days=1)

    # --- balances --------------------------------------------------------------
    def balance(self, account_key: str) -> int:
        """Same rule as nessie_tools.compute_balance (D18): opening + completed
        deposits - completed withdrawals - completed purchases."""
        total = self.plan.account(account_key).opening_balance
        for tx in self._txs_by_account[account_key]:
            if tx.status is not TxStatus.COMPLETED:
                continue
            total += tx.amount if tx.kind is TxKind.DEPOSIT else -tx.amount
        return total

    def pending_debits(self, account_key: str) -> int:
        return sum(
            tx.amount
            for tx in self._txs_by_account[account_key]
            if tx.status is TxStatus.PENDING and tx.kind is not TxKind.DEPOSIT
        )

    def amount_owed(self, card_key: str) -> int:
        """D45: a card's debt as a positive number."""
        if self.plan.account(card_key).type is not AccountType.CREDIT_CARD:
            raise ValueError(f"{card_key} is not a credit card")
        return -self.balance(card_key)

    # --- spending and income ---------------------------------------------------
    def spend(
        self,
        customer_key: str,
        start: date,
        end: date,
        *,
        category: str | None = None,
        merchant_key: str | None = None,
        account_key: str | None = None,
        include_pending: bool = False,
    ) -> Spend:
        accounts = {a.key for a in self.plan.accounts_of(customer_key)}
        if account_key is not None:
            if account_key not in accounts:
                raise ValueError(f"{account_key} does not belong to {customer_key}")
            accounts = {account_key}
        total = count = 0
        for key in sorted(accounts):
            for tx in self._txs_by_account[key]:
                if tx.kind is not TxKind.PURCHASE or tx.internal:
                    continue
                if not start <= tx.day <= end:
                    continue
                if tx.status is not TxStatus.COMPLETED and not (
                    include_pending and tx.status is TxStatus.PENDING
                ):
                    continue
                if merchant_key is not None and tx.merchant_key != merchant_key:
                    continue
                if category is not None:
                    if tx.merchant_key is None:
                        continue
                    if self.plan.merchant(tx.merchant_key).category != category:
                        continue
                total += tx.amount
                count += 1
        return Spend(total, count)

    def spend_by_category(self, customer_key: str, start: date, end: date) -> dict[str, int]:
        totals: dict[str, int] = defaultdict(int)
        for key in sorted(a.key for a in self.plan.accounts_of(customer_key)):
            for tx in self._txs_by_account[key]:
                if (
                    tx.kind is TxKind.PURCHASE
                    and not tx.internal
                    and tx.status is TxStatus.COMPLETED
                    and start <= tx.day <= end
                    and tx.merchant_key is not None
                ):
                    totals[self.plan.merchant(tx.merchant_key).category] += tx.amount
        return dict(sorted(totals.items()))

    def income(self, customer_key: str, start: date, end: date) -> int:
        return sum(
            tx.amount
            for a in self.plan.accounts_of(customer_key)
            if a.type is AccountType.CHECKING
            for tx in self._txs_by_account[a.key]
            if tx.kind is TxKind.DEPOSIT
            and not tx.internal
            and tx.status is TxStatus.COMPLETED
            and start <= tx.day <= end
        )

    # --- bills -----------------------------------------------------------------
    def bills(self, customer_key: str, status: BillStatus | None = None) -> list[PlanBill]:
        accounts = {a.key for a in self.plan.accounts_of(customer_key)}
        return [
            b
            for b in self.plan.bills
            if b.account_key in accounts and (status is None or b.status is status)
        ]

    # --- human-readable summary ------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """A compact overview to eyeball (written next to the plan by the CLI)."""
        out: dict[str, Any] = {
            "seed": self.plan.seed,
            "today": self.today.isoformat(),
            "history": [self.plan.start.isoformat(), (self.today - timedelta(1)).isoformat()],
            "plan_digest": self.plan.digest(),
            "customers": {},
        }
        full = (self.plan.start, self.today - timedelta(days=1))
        for c in self.plan.customers:
            accounts = {}
            for a in self.plan.accounts_of(c.key):
                entry: dict[str, Any] = {"type": a.type.value, "opening": a.opening_balance}
                if a.type is AccountType.CREDIT_CARD:
                    entry["amount_owed"] = self.amount_owed(a.key)
                else:
                    entry["balance"] = self.balance(a.key)
                entry["pending_debits"] = self.pending_debits(a.key)
                accounts[a.key] = entry
            out["customers"][c.key] = {
                "name": f"{c.first_name} {c.last_name}",
                "persona": c.persona,
                "accounts": accounts,
                "income_90d": self.income(c.key, *full),
                "spend_by_category_90d": self.spend_by_category(c.key, *full),
                "spend_last_7d": self.spend(c.key, *self.last_n_days(7)).total,
                "spend_last_30d": self.spend(c.key, *self.last_n_days(30)).total,
                "upcoming_bills": [
                    {"payee": b.payee, "amount": b.amount, "due": b.payment_date.isoformat()}
                    for b in self.bills(c.key, BillStatus.PENDING)
                ],
                "transactions": sum(
                    1 for t in self.plan.transactions if t.account_key.startswith(f"{c.key}/")
                ),
            }
        return out
