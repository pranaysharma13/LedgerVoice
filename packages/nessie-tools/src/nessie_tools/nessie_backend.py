"""BankBackend implementation for the live Nessie API.

Every Nessie quirk we found in M0 is handled here, and only here:
- create responses wrap the new object: {"code": 201, "objectCreated": {...}}
- the server only returns some fields on create, so we merge in what we sent
- purchases use `purchase_date` (the docs wrongly say transaction_date)
- merchant `category` is a single string
- some empty lists come back as 404 "No <things> found for this account" -> []
- no referential integrity (D36): lists don't check that the account exists, and
  creates succeed even for an account id that doesn't exist. Callers that need to
  know whether an account exists must call get_account (the saga and reconcile do).
- amounts are integers; non-integers would be silently truncated (D16 gate)
- responses that don't match our models become NessieError, never a raw pydantic
  error, so callers (and the saga) only ever see BankError subclasses (R1-04)
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from nessie_tools.client import NessieClient
from nessie_tools.errors import BankError, NessieError, NotFoundError
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

M = TypeVar("M", bound=BaseModel)

# Nessie's way of saying "empty list" on some endpoints, e.g.
# "No transfers found for this account".
_EMPTY_LIST_404 = re.compile(r"No \w+ found for this (account|customer)", re.IGNORECASE)


class NessieBackend:
    def __init__(self, client: NessieClient) -> None:
        self._c = client

    # --- helpers ---------------------------------------------------------------
    @staticmethod
    def _parse(model: type[M], data: Any, where: str) -> M:
        """Validate one object; bad data becomes a NessieError, never a pydantic error.

        status_code=None marks it ambiguous: for a create, the server accepted the
        write before we failed to read the response (R1-04).
        """
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            errors = exc.errors()
            reason = errors[0].get("msg", "invalid") if errors else "invalid"
            raise NessieError(f"{where}: unexpected {model.__name__} data ({reason})") from None

    def _get(self, path: str) -> Any:
        try:
            return self._c.request("GET", path)
        except NessieError as exc:
            if exc.status_code == 404:
                raise NotFoundError(f"GET {path}: not found") from None
            raise

    def _list(self, path: str, model: type[M], *, skip_invalid: bool = False) -> list[M]:
        try:
            data = self._c.request("GET", path)
        except NessieError as exc:
            if exc.status_code == 404 and _EMPTY_LIST_404.search(exc.body or ""):
                return []
            if exc.status_code == 404:
                raise NotFoundError(f"GET {path}: not found") from None
            raise
        if not isinstance(data, list):
            raise NessieError(f"GET {path}: expected a list, got {type(data).__name__}")
        items: list[M] = []
        for item in data:
            try:
                items.append(self._parse(model, item, f"GET {path}"))
            except NessieError:
                if not skip_invalid:
                    raise
        return items

    def _create(
        self,
        path: str,
        payload: dict[str, Any],
        model: type[M],
        known: dict[str, Any] | None = None,
    ) -> M:
        """POST and parse the created object.

        `known` holds facts we know but did not send (e.g. the owning customer_id).
        Errors after the server accepted the write are raised as ambiguous.
        """
        try:
            resp = self._c.request("POST", path, json=payload)
        except NessieError as exc:
            if exc.status_code == 404:
                raise NotFoundError(f"POST {path}: parent not found") from None
            raise
        obj = resp.get("objectCreated") if isinstance(resp, dict) else None
        if not isinstance(obj, dict):
            obj = resp if isinstance(resp, dict) else None
        if not obj or not (obj.get("_id") or obj.get("id")):
            raise NessieError(f"POST {path}: response has no created object id")
        # The create response may omit fields; what we sent fills the gaps,
        # and anything the server returned wins.
        return self._parse(model, {**(known or {}), **payload, **obj}, f"POST {path}")

    # --- customers -------------------------------------------------------------
    def create_customer(self, first_name: str, last_name: str, address: Address) -> Customer:
        payload = {
            "first_name": first_name,
            "last_name": last_name,
            "address": address.model_dump(),
        }
        return self._create("/customers", payload, Customer)

    # --- accounts --------------------------------------------------------------
    def create_account(
        self, customer_id: str, type: AccountType, nickname: str, balance: int
    ) -> Account:
        payload = {
            "type": type.value,
            "nickname": nickname,
            "rewards": 0,
            "balance": require_whole_dollars(balance, allow_zero=True),
        }
        account = self._create(
            f"/customers/{customer_id}/accounts", payload, Account, {"customer_id": customer_id}
        )
        if not account.account_number:  # create response may omit it; read it back
            try:
                account = self.get_account(account.id)
            except BankError:
                pass  # keep the account we have: losing its id would orphan it (R1-19)
        return account

    def list_accounts(self, customer_id: str) -> list[Account]:
        return self._list(f"/customers/{customer_id}/accounts", Account)

    def get_account(self, account_id: str) -> Account:
        path = f"/accounts/{account_id}"
        return self._parse(Account, self._get(path), f"GET {path}")

    def delete_account(self, account_id: str) -> None:
        try:
            self._c.request("DELETE", f"/accounts/{account_id}")
        except NessieError as exc:
            if exc.status_code == 404:
                raise NotFoundError(f"account {account_id} not found") from None
            raise

    # --- merchants -------------------------------------------------------------
    def create_merchant(self, name: str, category: str) -> Merchant:
        return self._create("/merchants", {"name": name, "category": category}, Merchant)

    def get_merchant(self, merchant_id: str) -> Merchant:
        path = f"/merchants/{merchant_id}"
        return self._parse(Merchant, self._get(path), f"GET {path}")

    def list_merchants(self) -> list[Merchant]:
        # The merchant list is shared sandbox data: skip entries we can't read.
        return self._list("/merchants", Merchant, skip_invalid=True)

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
        payload = {
            "merchant_id": merchant_id,
            "medium": "balance",
            "amount": require_whole_dollars(amount),
            "purchase_date": purchase_date.isoformat(),
            "description": description,
            "status": status.value,
        }
        return self._create(f"/accounts/{account_id}/purchases", payload, Purchase)

    def list_purchases(self, account_id: str) -> list[Purchase]:
        return self._list(f"/accounts/{account_id}/purchases", Purchase)

    def create_withdrawal(
        self,
        account_id: str,
        amount: int,
        transaction_date: date,
        description: str,
        status: TxStatus = TxStatus.COMPLETED,
    ) -> Withdrawal:
        payload = {
            "medium": "balance",
            "amount": require_whole_dollars(amount),
            "transaction_date": transaction_date.isoformat(),
            "description": description,
            "status": status.value,
        }
        return self._create(f"/accounts/{account_id}/withdrawals", payload, Withdrawal)

    def list_withdrawals(self, account_id: str) -> list[Withdrawal]:
        return self._list(f"/accounts/{account_id}/withdrawals", Withdrawal)

    def create_deposit(
        self,
        account_id: str,
        amount: int,
        transaction_date: date,
        description: str,
        status: TxStatus = TxStatus.COMPLETED,
    ) -> Deposit:
        payload = {
            "medium": "balance",
            "amount": require_whole_dollars(amount),
            "transaction_date": transaction_date.isoformat(),
            "description": description,
            "status": status.value,
        }
        return self._create(f"/accounts/{account_id}/deposits", payload, Deposit)

    def list_deposits(self, account_id: str) -> list[Deposit]:
        return self._list(f"/accounts/{account_id}/deposits", Deposit)

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
        payload: dict[str, Any] = {
            "status": status,
            "payee": payee,
            "nickname": nickname,
            "payment_date": payment_date.isoformat(),
            "payment_amount": require_whole_dollars(payment_amount),
        }
        if recurring_date is not None:
            payload["recurring_date"] = recurring_date
        return self._create(f"/accounts/{account_id}/bills", payload, Bill)

    def list_bills(self, account_id: str) -> list[Bill]:
        return self._list(f"/accounts/{account_id}/bills", Bill)
