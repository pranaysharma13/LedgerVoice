from datetime import date

from nessie_tools import TxStatus, compute_balance

DAY = date(2026, 11, 1)


def test_opening_balance_when_no_transactions(bank):
    b = compute_balance(bank.fake, bank.checking.id)
    assert (b.opening, b.current) == (1000, 1000)


def test_formula_counts_completed_items(bank):
    acc = bank.checking.id
    merchant = bank.fake.create_merchant("Blue Door Coffee", "coffee")
    bank.fake.create_deposit(acc, 200, DAY, "salary")
    bank.fake.create_withdrawal(acc, 50, DAY, "atm")
    bank.fake.create_purchase(acc, merchant.id, 7, DAY, "latte")

    b = compute_balance(bank.fake, acc)
    assert (b.deposits, b.withdrawals, b.purchases) == (200, 50, 7)
    assert b.current == 1000 + 200 - 50 - 7


def test_pending_and_cancelled_items_do_not_count(bank):
    acc = bank.checking.id
    merchant = bank.fake.create_merchant("Harvest Market", "groceries")
    bank.fake.create_purchase(acc, merchant.id, 30, DAY, "pending", TxStatus.PENDING)
    bank.fake.create_withdrawal(acc, 40, DAY, "cancelled", TxStatus.CANCELLED)
    bank.fake.create_deposit(acc, 99, DAY, "pending deposit", TxStatus.PENDING)

    assert compute_balance(bank.fake, acc).current == 1000


def test_stored_balance_is_not_used_as_current(bank):
    """Nessie's stored balance never moves (D18); only the computed one does."""
    bank.fake.create_withdrawal(bank.checking.id, 100, DAY, "x")
    assert bank.fake.get_account(bank.checking.id).balance == 1000
    assert compute_balance(bank.fake, bank.checking.id).current == 900
