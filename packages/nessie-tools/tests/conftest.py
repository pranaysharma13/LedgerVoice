"""Shared fixtures for the domain tests: a fake bank with one customer and two accounts."""

from dataclasses import dataclass
from datetime import date

import pytest
from nessie_tools import (
    Account,
    AccountType,
    Address,
    AuditLog,
    FakeBackend,
    TransferService,
)

ADDRESS = Address(
    street_number="1", street_name="Test St", city="Arlington", state="VA", zip="22201"
)
TODAY = date(2026, 11, 19)


@dataclass
class Bank:
    fake: FakeBackend
    audit: AuditLog
    service: TransferService
    customer_id: str
    checking: Account
    savings: Account
    other_customer_account: Account


@pytest.fixture
def bank():
    fake = FakeBackend(seed=3)
    audit = AuditLog(":memory:")
    ids = iter(f"LV-TRF-{n:08x}" for n in range(1, 1000))
    service = TransferService(fake, audit, today=lambda: TODAY, new_id=lambda: next(ids))
    alice = fake.create_customer("Alice", "Test", ADDRESS)
    bob = fake.create_customer("Bob", "Other", ADDRESS)
    checking = fake.create_account(alice.id, AccountType.CHECKING, "Checking", 1000)
    savings = fake.create_account(alice.id, AccountType.SAVINGS, "Savings", 500)
    bobs = fake.create_account(bob.id, AccountType.CHECKING, "Bob Checking", 100)
    yield Bank(fake, audit, service, alice.id, checking, savings, bobs)
    audit.close()
