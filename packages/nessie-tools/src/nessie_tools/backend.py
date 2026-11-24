"""The BankBackend interface (decision D20).

Plain storage operations only: no balances, no transfers, no business rules. Those
live one layer up (balances.py, transfers.py in M1b) and are written once, so tests
against FakeBackend exercise the real business logic.

Two implementations:
- NessieBackend: the live Nessie API
- FakeBackend:   in-memory, same behavior (including Nessie's quirks), plus
                 failure injection for tests

Rules shared by both:
- Every amount passes require_whole_dollars() before anything is stored (D16).
- get_account / get_merchant / delete_account on an unknown id raise NotFoundError.
- List operations on an account with no items return [].
- Creating a transaction never changes the account's stored balance (Nessie quirk).
- No referential integrity (D36, Nessie quirk): creates and lists do NOT check that
  the account exists, and deleting an account keeps its transactions. Code that must
  know whether an account exists calls get_account explicitly.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from nessie_tools.models import (
    Account,
    AccountType,
    Address,
    Bill,
    BillStatus,
    Customer,
    Deposit,
    Merchant,
    Purchase,
    TxStatus,
    Withdrawal,
)


class BankBackend(Protocol):
    # --- customers -------------------------------------------------------------
    def create_customer(self, first_name: str, last_name: str, address: Address) -> Customer: ...

    def get_customer(self, customer_id: str) -> Customer: ...  # NotFoundError if unknown

    # --- accounts --------------------------------------------------------------
    def create_account(
        self, customer_id: str, type: AccountType, nickname: str, balance: int
    ) -> Account: ...

    def list_accounts(self, customer_id: str) -> list[Account]: ...

    def get_account(self, account_id: str) -> Account: ...

    def delete_account(self, account_id: str) -> None: ...

    # --- merchants -------------------------------------------------------------
    def create_merchant(self, name: str, category: str) -> Merchant: ...

    def get_merchant(self, merchant_id: str) -> Merchant: ...

    def list_merchants(self) -> list[Merchant]: ...

    # --- transactions ----------------------------------------------------------
    def create_purchase(
        self,
        account_id: str,
        merchant_id: str,
        amount: int,
        purchase_date: date,
        description: str,
        status: TxStatus = TxStatus.COMPLETED,
    ) -> Purchase: ...

    def list_purchases(self, account_id: str) -> list[Purchase]: ...

    def create_withdrawal(
        self,
        account_id: str,
        amount: int,
        transaction_date: date,
        description: str,
        status: TxStatus = TxStatus.COMPLETED,
    ) -> Withdrawal: ...

    def list_withdrawals(self, account_id: str) -> list[Withdrawal]: ...

    def create_deposit(
        self,
        account_id: str,
        amount: int,
        transaction_date: date,
        description: str,
        status: TxStatus = TxStatus.COMPLETED,
    ) -> Deposit: ...

    def list_deposits(self, account_id: str) -> list[Deposit]: ...

    # --- bills -----------------------------------------------------------------
    def create_bill(
        self,
        account_id: str,
        payee: str,
        nickname: str,
        payment_amount: int,
        payment_date: date,
        recurring_date: int,  # required: see require_recurring_day (D49)
        status: BillStatus = BillStatus.PENDING,
    ) -> Bill: ...

    def list_bills(self, account_id: str) -> list[Bill]: ...
