"""FakeBackend-only behavior. Shared behavior is in tests/test_contract.py."""

from datetime import date

import pytest
from nessie_tools import (
    AccountType,
    Address,
    BackendFailure,
    FakeBackend,
    is_ambiguous,
)

ADDRESS = Address(
    street_number="1", street_name="Test St", city="Arlington", state="VA", zip="22201"
)
DAY = date(2026, 11, 1)


@pytest.fixture
def fake():
    return FakeBackend(seed=7)


@pytest.fixture
def account_id(fake):
    customer = fake.create_customer("Ada", "Test", ADDRESS)
    return fake.create_account(customer.id, AccountType.CHECKING, "Main", 1000).id


def test_same_seed_gives_same_ids_and_account_numbers():
    def build(seed):
        fb = FakeBackend(seed=seed)
        c = fb.create_customer("Ada", "Test", ADDRESS)
        a = fb.create_account(c.id, AccountType.CHECKING, "Main", 100)
        return c.id, a.id, a.account_number

    assert build(1) == build(1)
    assert build(1) != build(2)


def test_injected_failure_before_write_leaves_nothing(fake, account_id):
    fake.fail_next("create_deposit")
    with pytest.raises(BackendFailure) as info:
        fake.create_deposit(account_id, 50, DAY, "x")
    assert not is_ambiguous(info.value)
    assert fake.list_deposits(account_id) == []


def test_ambiguous_failure_writes_then_raises(fake, account_id):
    fake.fail_next("create_withdrawal", ambiguous=True)
    with pytest.raises(BackendFailure) as info:
        fake.create_withdrawal(account_id, 50, DAY, "LV-TRF-test")
    assert is_ambiguous(info.value)
    assert [w.description for w in fake.list_withdrawals(account_id)] == ["LV-TRF-test"]


def test_failures_queue_and_then_stop(fake, account_id):
    fake.fail_next("create_deposit")
    fake.fail_next("create_deposit")
    for _ in range(2):
        with pytest.raises(BackendFailure):
            fake.create_deposit(account_id, 5, DAY, "x")
    fake.create_deposit(account_id, 5, DAY, "ok")  # third call succeeds
    assert len(fake.list_deposits(account_id)) == 1


def test_fail_next_rejects_unknown_operation(fake):
    with pytest.raises(ValueError):
        fake.fail_next("create_transfer")
