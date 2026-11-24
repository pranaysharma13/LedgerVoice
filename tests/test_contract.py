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
    BillStatus,
    FakeBackend,
    Merchant,
    NessieBackend,
    NessieClient,
    NessieError,
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

    created: list[Account] = []
    try:
        customer = backend.create_customer("Casey", "Contract", ADDRESS)
        created.append(
            backend.create_account(customer.id, AccountType.CHECKING, "Contract Checking", 1000)
        )
        created.append(
            backend.create_account(customer.id, AccountType.SAVINGS, "Contract Savings", 500)
        )
        existing = [m for m in backend.list_merchants() if m.name == CONTRACT_MERCHANT]
        merchant = existing[0] if existing else backend.create_merchant(CONTRACT_MERCHANT, "coffee")
        yield World(backend, created[0], created[1], merchant)
    finally:
        for account in created:  # setup is inside the try: nothing leaks (R1-17)
            try:
                backend.delete_account(account.id)
            except NotFoundError:
                pass  # a test already deleted it
        if client is not None:
            client.close()


def _fresh_account(world: World) -> Account:
    return world.backend.create_account(
        world.checking.customer_id, AccountType.SAVINGS, "Contract Temp", 0
    )


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
    b = world.backend
    acc = _fresh_account(world).id  # its own account: no dependence on test order
    try:
        assert b.list_purchases(acc) == []
        assert b.list_withdrawals(acc) == []
        assert b.list_deposits(acc) == []
        assert b.list_bills(acc) == []
    finally:
        b.delete_account(acc)


def test_customer_without_accounts_lists_empty(world: World) -> None:
    customer = world.backend.create_customer("Nolan", "Noaccounts", ADDRESS)
    assert world.backend.list_accounts(customer.id) == []


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
    assert found[0].status is BillStatus.PENDING


@pytest.mark.parametrize("status", list(BillStatus))
def test_every_bill_status_round_trips(world: World, status: BillStatus) -> None:
    """D47: Nessie accepts exactly these four statuses and stores them as sent."""
    b, acc = world.backend, world.checking.id
    created = b.create_bill(acc, "Contract Water", f"water-{status}", 30, DAY, 9, status=status)
    [found] = [x for x in b.list_bills(acc) if x.id == created.id]
    assert found.status is status


def test_unknown_bill_status_never_reaches_the_bank(world: World) -> None:
    b, acc = world.backend, world.checking.id
    count = len(b.list_bills(acc))
    with pytest.raises(ValueError):
        b.create_bill(acc, "Contract Gas", "gas", 40, DAY, 9, status="paid")  # type: ignore[arg-type]
    assert len(b.list_bills(acc)) == count


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
    extra = _fresh_account(world)
    b.delete_account(extra.id)
    with pytest.raises(NotFoundError):
        b.get_account(extra.id)


def test_deleting_an_account_keeps_its_transactions(world: World) -> None:
    """D36 (verified live 2026-11-20): no referential integrity. The account goes away,
    its transactions stay, and listing them still works. Reconcile relies on this to
    keep checking legs of deleted accounts."""
    b = world.backend
    extra = _fresh_account(world)
    deposit = b.create_deposit(extra.id, 5, DAY, "before delete")
    b.delete_account(extra.id)
    with pytest.raises(NotFoundError):
        b.get_account(extra.id)
    assert [d.id for d in b.list_deposits(extra.id)] == [deposit.id]  # orphaned, still listed
    assert b.list_withdrawals(extra.id) == []


def test_bank_accepts_transactions_for_unknown_accounts(world: World) -> None:
    """D36: Nessie stores a deposit for an account id that never existed. This is why
    the saga checks both accounts with get_account before writing any leg."""
    b = world.backend
    ghost = str(uuid.uuid4())
    deposit = b.create_deposit(ghost, 5, DAY, "to nowhere")
    assert [d.id for d in b.list_deposits(ghost)] == [deposit.id]
    with pytest.raises(NotFoundError):
        b.get_account(ghost)


@pytest.mark.parametrize("day", [None, 0, 32, True])
def test_bill_without_a_valid_recurring_day_never_reaches_the_bank(
    world: World, day: object
) -> None:
    """D49: a bill without recurring_date would make this account's bill list unreadable
    on Nessie, so both backends refuse it before sending anything."""
    b, acc = world.backend, world.checking.id
    count = len(b.list_bills(acc))
    with pytest.raises(ValueError, match="recurring_date"):
        b.create_bill(acc, "Contract Gas", "gas", 40, DAY, day)  # type: ignore[arg-type]
    assert len(b.list_bills(acc)) == count


@pytest.mark.live
def test_nessie_bill_without_recurring_date_breaks_the_bill_list() -> None:
    """D49, documented on the live server (2026-11-23): the create is accepted, then every
    list of that account's bills fails with HTTP 400. Uses a throwaway account under a
    made-up customer id (accepted, D47) and the raw client to bypass our own check; the
    account is deleted afterwards. If this test starts failing, Nessie fixed the bug and
    D49 can be relaxed."""
    backend, client = _live_backend()
    account = backend.create_account(str(uuid.uuid4()), AccountType.CHECKING, "Contract D49", 0)
    try:
        backend.create_bill(account.id, "Contract Power", "ok", 10, DAY, 5)
        assert len(backend.list_bills(account.id)) == 1  # listing works with a valid bill
        payload = {
            "status": "pending",
            "payee": "Contract Power",
            "nickname": "no-recurring-date",
            "payment_date": DAY.isoformat(),
            "payment_amount": 10,
        }
        client.request("POST", f"/accounts/{account.id}/bills", json=payload)  # accepted
        with pytest.raises(NessieError) as info:
            backend.list_bills(account.id)
        assert info.value.status_code == 400
        assert "recurring_date" in (info.value.body or "")
    finally:
        backend.delete_account(account.id)
        client.close()


def test_customer_round_trip(world: World) -> None:
    """M1c-2 reuses seed customers across reseeds, so it must be able to look them up."""
    b = world.backend
    customer = b.create_customer("Casey", "Contract", ADDRESS)
    found = b.get_customer(customer.id)
    assert (found.id, found.first_name, found.last_name) == (customer.id, "Casey", "Contract")
    with pytest.raises(NotFoundError):
        b.get_customer(str(uuid.uuid4()))


def test_bank_accepts_an_account_for_an_unknown_customer(world: World) -> None:
    """D47 (verified live 2026-11-23): account creation doesn't check the customer.
    The seeder must only use customer ids it created itself."""
    b = world.backend
    ghost_customer = str(uuid.uuid4())
    account = b.create_account(ghost_customer, AccountType.CHECKING, "Contract Ghost", 0)
    try:
        assert b.get_account(account.id).id == account.id
    finally:
        b.delete_account(account.id)


def test_bank_accepts_a_purchase_with_an_unknown_merchant(world: World) -> None:
    """D47 (verified live 2026-11-23): the purchase is stored, but its merchant (and so
    its category) can't be looked up. Seeding verifies merchants; spending tools must
    report such purchases as category "unknown", never drop them."""
    b, acc = world.backend, world.checking.id
    ghost_merchant = str(uuid.uuid4())
    purchase = b.create_purchase(acc, ghost_merchant, 3, DAY, "unknown merchant")
    assert [p.id for p in b.list_purchases(acc) if p.id == purchase.id] == [purchase.id]
    with pytest.raises(NotFoundError):
        b.get_merchant(ghost_merchant)
