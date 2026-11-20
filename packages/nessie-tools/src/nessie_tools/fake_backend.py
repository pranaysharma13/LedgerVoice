"""In-memory BankBackend that behaves like Nessie (decision D20).

Used by unit tests, the default eval run, and offline development: fast, free and
resettable. It deliberately copies Nessie's behavior, including the quirks:
- creating a transaction never changes the account's stored balance
- no referential integrity (D36, verified live 2026-11-20): deleting an account
  keeps its transactions; transactions and bills can be created for an account id
  that doesn't exist; listing an unknown account returns what is stored, or []
- get/delete of an unknown account or merchant raises NotFoundError

It adds one thing Nessie doesn't have: failure injection, so tests can make a
specific operation fail on purpose, including a "late write" that lands after the
caller has already given up and looked (see fail_next).
"""

from __future__ import annotations

import random
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import TypeVar

from nessie_tools.errors import BackendFailure, NotFoundError
from nessie_tools.models import (
    Account,
    AccountType,
    Address,
    Bill,
    Customer,
    Deposit,
    Merchant,
    Purchase,
    TxStatus,
    Withdrawal,
)
from nessie_tools.money import require_whole_dollars

T = TypeVar("T")


@dataclass
class _PlannedFailure:
    ambiguous: bool
    message: str
    appears_after_lists: int | None = None


@dataclass
class _LateWrite:
    remaining_lists: int
    write: Callable[[], object]


@dataclass
class _Store:
    customers: dict[str, Customer] = field(default_factory=dict)
    accounts: dict[str, Account] = field(default_factory=dict)
    merchants: dict[str, Merchant] = field(default_factory=dict)
    purchases: dict[str, list[Purchase]] = field(default_factory=lambda: defaultdict(list))
    withdrawals: dict[str, list[Withdrawal]] = field(default_factory=lambda: defaultdict(list))
    deposits: dict[str, list[Deposit]] = field(default_factory=lambda: defaultdict(list))
    bills: dict[str, list[Bill]] = field(default_factory=lambda: defaultdict(list))


class FakeBackend:
    def __init__(self, seed: int = 0) -> None:
        # A seeded RNG keeps ids and account numbers identical between runs.
        self._rng = random.Random(seed)
        self._store = _Store()
        self._failures: dict[str, list[_PlannedFailure]] = defaultdict(list)
        self._late_writes: list[_LateWrite] = []

    # --- failure injection (test-only feature) -----------------------------------
    def fail_next(
        self,
        operation: str,
        *,
        ambiguous: bool = False,
        appears_after_lists: int | None = None,
    ) -> None:
        """Make the next call to `operation` (e.g. "create_deposit") fail.

        ambiguous=False: the call fails BEFORE writing anything (like an HTTP 4xx).
        ambiguous=True:  the write HAPPENS, then the call raises (like a timeout after
                         the server processed the request).
        appears_after_lists=n: a LATE write. The call raises an ambiguous error
                         WITHOUT writing; the write appears after n more list_* calls
                         (any account). Models a server that finishes the request
                         after the client timed out (R1-02). Use flush_late_writes()
                         to make all pending late writes land now.
        Calls can be queued: fail_next twice makes the next two calls fail.
        """
        if not hasattr(self, operation):
            raise ValueError(f"unknown operation {operation!r}")
        if appears_after_lists is not None:
            planned = _PlannedFailure(
                True, f"injected timeout in {operation} (write lands late)", appears_after_lists
            )
        else:
            label = "after writing (ambiguous)" if ambiguous else "before writing"
            planned = _PlannedFailure(ambiguous, f"injected failure in {operation} {label}")
        self._failures[operation].append(planned)

    def flush_late_writes(self) -> None:
        """Apply every pending late write now."""
        pending, self._late_writes = self._late_writes, []
        for late in pending:
            late.write()

    def _run(self, operation: str, write: Callable[[], T]) -> T:
        planned = self._failures[operation].pop(0) if self._failures[operation] else None
        if planned is None:
            return write()
        if planned.appears_after_lists is not None:
            self._late_writes.append(_LateWrite(planned.appears_after_lists, write))
        elif planned.ambiguous:
            write()
        raise BackendFailure(planned.message, ambiguous=planned.ambiguous)

    def _tick_late_writes(self) -> None:
        """Called at the start of every list_* call: late writes count down and land."""
        still_pending: list[_LateWrite] = []
        for late in self._late_writes:
            if late.remaining_lists <= 0:
                late.write()
            else:
                late.remaining_lists -= 1
                still_pending.append(late)
        self._late_writes = still_pending

    # --- helpers ---------------------------------------------------------------
    def _new_id(self) -> str:
        return str(uuid.UUID(int=self._rng.getrandbits(128), version=4))

    def _account_number(self) -> str:
        return "".join(str(self._rng.randint(0, 9)) for _ in range(16))

    def _require_account(self, account_id: str) -> Account:
        try:
            return self._store.accounts[account_id]
        except KeyError:
            raise NotFoundError(f"account {account_id} not found") from None

    def _require_merchant(self, merchant_id: str) -> Merchant:
        try:
            return self._store.merchants[merchant_id]
        except KeyError:
            raise NotFoundError(f"merchant {merchant_id} not found") from None

    # --- customers -------------------------------------------------------------
    def create_customer(self, first_name: str, last_name: str, address: Address) -> Customer:
        def write() -> Customer:
            customer = Customer(id=self._new_id(), first_name=first_name, last_name=last_name)
            self._store.customers[customer.id] = customer
            return customer

        return self._run("create_customer", write)

    # --- accounts --------------------------------------------------------------
    def create_account(
        self, customer_id: str, type: AccountType, nickname: str, balance: int
    ) -> Account:
        balance = require_whole_dollars(balance, allow_zero=True)
        if customer_id not in self._store.customers:
            raise NotFoundError(f"customer {customer_id} not found")

        def write() -> Account:
            account = Account(
                id=self._new_id(),
                type=type,
                nickname=nickname,
                balance=balance,
                account_number=self._account_number(),
                customer_id=customer_id,
            )
            self._store.accounts[account.id] = account
            return account

        return self._run("create_account", write)

    def list_accounts(self, customer_id: str) -> list[Account]:
        self._tick_late_writes()
        return [a for a in self._store.accounts.values() if a.customer_id == customer_id]

    def get_account(self, account_id: str) -> Account:
        return self._require_account(account_id)

    def delete_account(self, account_id: str) -> None:
        self._require_account(account_id)

        def write() -> None:
            # Like Nessie: the account goes, its transactions and bills stay (orphans).
            del self._store.accounts[account_id]

        self._run("delete_account", write)

    # --- merchants -------------------------------------------------------------
    def create_merchant(self, name: str, category: str) -> Merchant:
        def write() -> Merchant:
            merchant = Merchant(id=self._new_id(), name=name, category=category)
            self._store.merchants[merchant.id] = merchant
            return merchant

        return self._run("create_merchant", write)

    def get_merchant(self, merchant_id: str) -> Merchant:
        return self._require_merchant(merchant_id)

    def list_merchants(self) -> list[Merchant]:
        self._tick_late_writes()
        return list(self._store.merchants.values())

    # --- transactions ----------------------------------------------------------
    def create_purchase(
        self,
        account_id: str,
        merchant_id: str,
        amount: int,
        purchase_date: date,
        description: str,
        status: TxStatus = TxStatus.COMPLETED,
    ) -> Purchase:
        amount = require_whole_dollars(amount)
        self._require_merchant(merchant_id)

        def write() -> Purchase:
            purchase = Purchase(
                id=self._new_id(),
                merchant_id=merchant_id,
                amount=amount,
                purchase_date=purchase_date,
                description=description,
                status=status,
            )
            self._store.purchases[account_id].append(purchase)
            return purchase

        return self._run("create_purchase", write)

    def list_purchases(self, account_id: str) -> list[Purchase]:
        self._tick_late_writes()
        return list(self._store.purchases[account_id])

    def create_withdrawal(
        self,
        account_id: str,
        amount: int,
        transaction_date: date,
        description: str,
        status: TxStatus = TxStatus.COMPLETED,
    ) -> Withdrawal:
        amount = require_whole_dollars(amount)

        def write() -> Withdrawal:
            withdrawal = Withdrawal(
                id=self._new_id(),
                amount=amount,
                transaction_date=transaction_date,
                description=description,
                status=status,
            )
            self._store.withdrawals[account_id].append(withdrawal)
            return withdrawal

        return self._run("create_withdrawal", write)

    def list_withdrawals(self, account_id: str) -> list[Withdrawal]:
        self._tick_late_writes()
        return list(self._store.withdrawals[account_id])

    def create_deposit(
        self,
        account_id: str,
        amount: int,
        transaction_date: date,
        description: str,
        status: TxStatus = TxStatus.COMPLETED,
    ) -> Deposit:
        amount = require_whole_dollars(amount)

        def write() -> Deposit:
            deposit = Deposit(
                id=self._new_id(),
                amount=amount,
                transaction_date=transaction_date,
                description=description,
                status=status,
            )
            self._store.deposits[account_id].append(deposit)
            return deposit

        return self._run("create_deposit", write)

    def list_deposits(self, account_id: str) -> list[Deposit]:
        self._tick_late_writes()
        return list(self._store.deposits[account_id])

    # --- bills -----------------------------------------------------------------
    def create_bill(
        self,
        account_id: str,
        payee: str,
        nickname: str,
        payment_amount: int,
        payment_date: date,
        recurring_date: int | None = None,
        status: str = "pending",
    ) -> Bill:
        payment_amount = require_whole_dollars(payment_amount)

        def write() -> Bill:
            bill = Bill(
                id=self._new_id(),
                payee=payee,
                nickname=nickname,
                payment_amount=payment_amount,
                payment_date=payment_date,
                recurring_date=recurring_date,
                status=status,
            )
            self._store.bills[account_id].append(bill)
            return bill

        return self._run("create_bill", write)

    def list_bills(self, account_id: str) -> list[Bill]:
        self._tick_late_writes()
        return list(self._store.bills[account_id])
