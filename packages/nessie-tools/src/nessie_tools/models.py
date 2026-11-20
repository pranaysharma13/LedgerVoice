"""Typed data models for Nessie objects.

Models ignore unknown fields (Nessie adds fields like `payer_id` that it does not
document), and are frozen, so a value read from the bank can't be changed by accident.
Nessie returns ids under `_id`; in Python we use `id`.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _parse_date(value: Any) -> Any:
    """Accept "2026-11-08" and also "2026-11-08T00:00:00..." by keeping the date part."""
    if isinstance(value, str) and len(value) >= 10:
        return value[:10]
    return value


def _parse_category(value: Any) -> Any:
    """Nessie stores one string, but its own docs (and older data) use a list."""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or None
    return value


NessieDate = Annotated[date, BeforeValidator(_parse_date)]
Category = Annotated[str | None, BeforeValidator(_parse_category)]


class AccountType(StrEnum):
    CHECKING = "Checking"
    SAVINGS = "Savings"
    CREDIT_CARD = "Credit Card"


class TxStatus(StrEnum):
    PENDING = "pending"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True)


class Address(_Model):
    street_number: str
    street_name: str
    city: str
    state: str
    zip: str


class Customer(_Model):
    id: str = Field(alias="_id")
    first_name: str
    last_name: str


class Account(_Model):
    id: str = Field(alias="_id")
    type: AccountType
    nickname: str
    balance: int  # stored "opening" balance: Nessie never updates it (D18)
    rewards: int = 0
    account_number: str = ""
    customer_id: str

    @property
    def last4(self) -> str:
        return self.account_number[-4:]


class Merchant(_Model):
    id: str = Field(alias="_id")
    name: str = ""  # the shared sandbox may hold merchants created by other tools
    category: Category = None


class Purchase(_Model):
    id: str = Field(alias="_id")
    merchant_id: str
    amount: int
    purchase_date: NessieDate | None = None
    description: str = ""
    status: TxStatus | None = None  # unknown status never counts toward balances
    medium: str = "balance"


class Withdrawal(_Model):
    id: str = Field(alias="_id")
    amount: int
    transaction_date: NessieDate | None = None
    description: str = ""
    status: TxStatus | None = None
    medium: str = "balance"


class Deposit(_Model):
    id: str = Field(alias="_id")
    amount: int
    transaction_date: NessieDate | None = None
    description: str = ""
    status: TxStatus | None = None
    medium: str = "balance"


class Bill(_Model):
    id: str = Field(alias="_id")
    payee: str
    nickname: str | None = None
    payment_amount: int
    payment_date: NessieDate | None = None
    recurring_date: int | None = None
    status: str = "pending"  # Nessie's bill status values are not documented; kept as text
