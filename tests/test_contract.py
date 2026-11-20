"""Contract tests: one suite, run against every BankBackend (decision D20).

If FakeBackend and NessieBackend both pass, the fake behaves like real Nessie for
everything we rely on, so tests and evals on the fake are meaningful.

- fake: always runs (offline, free)
- live: only with `make test-live` (real Nessie; creates a customer and accounts,
        deletes the accounts at the end; customers can't be deleted)
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date

import pytest
from nessie_tools import (
    Account,
    AccountType,
    Address,
    AmountError,
    BankBackend,
    FakeBackend,
    Merchant,
    NessieBackend,
    NessieClient,
    NotFoundError,
    TxStatus,
)

ADDRESS = Address(
    street_number="1", street_name="Contract St", city="Arlington", state="VA", zip="22201"
)
CONTRACT_MERCHANT = "LV Contract Coffee"  # reused across live runs (merchants can't be deleted)
DAY = date(2026, 11, 2)


@dataclass
class World:
    backend: BankBackend
    checking: Account
    savings: Account
    merchant: Merchant


def _live_backend() -> tuple[NessieBackend, NessieClient]:
    from ledgervoice.config import get_settings

    s = get_settings()
    s.require("nessie_api_key")
    assert s.nessie_api_key is not None
    client = NessieClient(s.nessie_api_key.get_secret_value(), base_url=s.nessie_base_url)
    return NessieBackend(client), client


@pytest.fixture(
    scope="module",
    params=["fake", pytest.param("live", marks=pytest.mark.live)],
)
def world(request: pytest.FixtureRequest) -> Iterator[World]:
    client: NessieClient | None = None
    backend: BankBackend
    if request.param == "fake":
        backend = FakeBackend(seed=1)
    else:
        backend, client = _live_backend()

    customer = backend.create_customer("Casey", "Contract", ADDRESS)
    checking = backend.create_account(customer.id, AccountType.CHECKING, "Contract Checking", 1000)
    savings = backend.create_account(customer.id, AccountType.SAVINGS, "Contract Savings", 500)
    existing = [m for m in backend.list_merchants() if m.name == CONTRACT_MERCHANT]
    merchant = existing[0] if existing else backend.create_merchant(CONTRACT_MERCHANT, "coffee")
    try:
        yield World(backend, checking, savings, merchant)
    finally:
        for account in (checking, savings):
            try:
                backend.delete_account(account.id)
            except NotFoundError:
                pass  # a test already deleted it
        if client is not None:
            client.close()


def test_accounts_round_trip(world: World) -> None:
    accounts = {a.id: a for a in world.backend.list_accounts(world.checking.customer_id)}
    assert set(accounts) == {world.checking.id, world.savings.id}
    chk = accounts[world.checking.id]
    assert chk.type is AccountType.CHECKING
    assert chk.nickname == "Contract Checking"
    assert chk.balance == 1000
    assert len(chk.last4) == 4 and chk.last4.isdigit()
    assert world.backend.get_account(world.savings.id).balance == 500


def test_new_account_lists_are_empty(world: World) -> None:
    b, acc = world.backend, world.savings.id
    assert b.list_purchases(acc) == []
    assert b.list_withdrawals(acc) == []
    assert b.list_deposits(acc) == []
    assert b.list_bills(acc) == []


def test_merchant_category_is_a_string(world: World) -> None:
    m = world.backend.get_merchant(world.merchant.id)
    assert m.name == CONTRACT_MERCHANT
    assert m.category == "coffee"


def test_backdated_purchase_round_trip(world: World) -> None:
    b, acc = world.backend, world.checking.id
    created = b.create_purchase(acc, world.merchant.id, 7, DAY, "contract latte")
    found = [p for p in b.list_purchases(acc) if p.id == created.id]
    assert len(found) == 1
    p = found[0]
    assert (p.amount, p.purchase_date, p.description) == (7, DAY, "contract latte")
    assert p.merchant_id == world.merchant.id
    assert p.status is TxStatus.COMPLETED


def test_withdrawal_and_deposit_round_trip_keep_transfer_id(world: World) -> None:
    b = world.backend
    tid = f"LV-TRF-contract-{uuid.uuid4().hex[:6]}"
    b.create_withdrawal(world.checking.id, 25, DAY, f"{tid} leg 1/2")
    b.create_deposit(world.savings.id, 25, DAY, f"{tid} leg 2/2")
    w = [x for x in b.list_withdrawals(world.checking.id) if x.description.startswith(tid)]
    d = [x for x in b.list_deposits(world.savings.id) if x.description.startswith(tid)]
    assert [(x.amount, x.transaction_date) for x in w] == [(25, DAY)]
    assert [(x.amount, x.transaction_date) for x in d] == [(25, DAY)]


def test_transactions_never_change_stored_balance(world: World) -> None:
    """The Nessie quirk behind D18: balances must be computed, not read."""
    b, acc = world.backend, world.checking.id
    before = b.get_account(acc).balance
    b.create_withdrawal(acc, 10, DAY, "quirk check")
    assert b.get_account(acc).balance == before


def test_bill_round_trip(world: World) -> None:
    b, acc = world.backend, world.checking.id
    created = b.create_bill(acc, "Contract Electric", "electric", 82, date(2026, 11, 25), 25)
    found = [x for x in b.list_bills(acc) if x.id == created.id]
    assert len(found) == 1
    assert (found[0].payee, found[0].payment_amount) == ("Contract Electric", 82)


def test_non_whole_amount_never_reaches_the_bank(world: World) -> None:
    b, acc = world.backend, world.checking.id
    count = len(b.list_withdrawals(acc))
    with pytest.raises(AmountError):
        b.create_withdrawal(acc, 4.75, DAY, "must not exist")  # type: ignore[arg-type]
    assert len(b.list_withdrawals(acc)) == count


def test_unknown_account_is_not_found(world: World) -> None:
    with pytest.raises(NotFoundError):
        world.backend.get_account(str(uuid.uuid4()))


def test_delete_account(world: World) -> None:
    b = world.backend
    extra = b.create_account(world.checking.customer_id, AccountType.SAVINGS, "Temp", 0)
    b.delete_account(extra.id)
    with pytest.raises(NotFoundError):
        b.get_account(extra.id)
