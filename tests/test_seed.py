"""M1c-1: seed generator and ground truth (offline).

The most important test is test_truth_matches_the_bank: it writes the plan into the
fake bank and checks that what nessie-tools computes from the bank (compute_balance,
spending joined with merchants, bills) equals the ground truth computed from the plan.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import pytest
from nessie_tools import (
    AccountType,
    BillStatus,
    TxStatus,
    compute_balance,
)

from ledgervoice.seed import (
    GroundTruth,
    PlanAccount,
    PlanCustomer,
    PlanMerchant,
    PlanTx,
    SeedConfigError,
    SeedPlan,
    TxKind,
    catalog,
    generate,
)
from ledgervoice.seed.__main__ import main as seed_cli
from ledgervoice.seed.apply import seed_fake

SEED, ANCHOR = 42, date(2026, 11, 23)
# Pinned on purpose: any change to the generator or the catalog changes the data every
# eval is scored against. If you changed them deliberately, update this digest and say
# so in the commit message.
GOLDEN_DIGEST = "0e6fb09adb75a2b43351b59dbde4e5d7ac95ee158f84f92119ddec02d155041c"


@pytest.fixture(scope="module")
def plan() -> SeedPlan:
    return generate(SEED, ANCHOR)


@pytest.fixture(scope="module")
def truth(plan: SeedPlan) -> GroundTruth:
    return GroundTruth(plan)


# --- determinism ---------------------------------------------------------------------
def test_same_inputs_give_identical_bytes(plan: SeedPlan) -> None:
    assert generate(SEED, ANCHOR).to_json() == plan.to_json()


def test_golden_digest(plan: SeedPlan) -> None:
    assert plan.digest() == GOLDEN_DIGEST


def test_digest_is_independent_of_python_hash_randomization() -> None:
    """The built-in hash() changes per process; the plan must not depend on it."""
    code = (
        "from datetime import date; from ledgervoice.seed import generate; "
        f"print(generate({SEED}, date({ANCHOR.year}, {ANCHOR.month}, {ANCHOR.day})).digest())"
    )
    digests = set()
    for hash_seed in ("1", "2", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
        out = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
        )
        digests.add(out.stdout.strip())
    assert digests == {GOLDEN_DIGEST}


def test_other_seeds_and_dates_give_other_plans(plan: SeedPlan) -> None:
    assert generate(SEED + 1, ANCHOR).digest() != plan.digest()
    later = generate(SEED, ANCHOR + timedelta(days=7))
    assert later.digest() != plan.digest()
    assert later.start == plan.start + timedelta(days=7)


def test_plan_json_has_no_floats(plan: SeedPlan) -> None:
    def walk(value: object) -> None:
        assert not isinstance(value, float)
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(json.loads(plan.to_json()))


# --- shape and money-flow rules (D48) -------------------------------------------------
def test_shape(plan: SeedPlan) -> None:
    assert [c.persona for c in plan.customers] == ["student", "professional", "traveller"]
    assert len(plan.accounts) == 9
    for c in plan.customers:
        types = sorted(a.type.value for a in plan.accounts_of(c.key))
        assert types == ["Checking", "Credit Card", "Savings"]
    assert {m.category for m in plan.merchants} <= set(catalog.CATEGORIES)
    keys = [m.key for m in plan.merchants] + [a.key for a in plan.accounts]
    keys += [t.key for t in plan.transactions] + [b.key for b in plan.bills]
    assert len(keys) == len(set(keys))


def test_transactions_are_whole_positive_dollars_inside_the_window(plan: SeedPlan) -> None:
    last = ANCHOR - timedelta(days=1)
    for tx in plan.transactions:
        assert isinstance(tx.amount, int) and tx.amount > 0, tx
        assert plan.start <= tx.day <= last, tx


def test_purchases_and_only_purchases_name_a_known_merchant(plan: SeedPlan) -> None:
    """D47: Nessie would accept an unknown merchant id; the plan must never contain one."""
    known = {m.key for m in plan.merchants}
    for tx in plan.transactions:
        if tx.kind is TxKind.PURCHASE:
            assert tx.merchant_key in known, tx
            assert tx.description == plan.merchant(tx.merchant_key).name
        else:
            assert tx.merchant_key is None, tx


def _assert_never_negative(plan: SeedPlan) -> None:
    """Checking and savings stay >= 0 on every day (pending debits count too: a real
    bank would hold them against the balance)."""
    for account in plan.accounts:
        if account.type is AccountType.CREDIT_CARD:
            continue
        running = account.opening_balance
        txs = sorted(
            (t for t in plan.transactions if t.account_key == account.key),
            key=lambda t: (t.day, t.kind is not TxKind.DEPOSIT),  # credits first each day
        )
        for tx in txs:
            running += tx.amount if tx.kind is TxKind.DEPOSIT else -tx.amount
            assert running >= 0, f"seed {plan.seed}: {account.key} below zero on {tx.day}"


def _assert_card_payments_follow_the_statement(plan: SeedPlan) -> list[tuple[int, int]]:
    """Recompute each statement independently: completed card purchases before the 1st
    of the payment month, minus earlier payments. A payment never exceeds it. Returns
    (paid, due) pairs."""
    pairs: list[tuple[int, int]] = []
    for account in plan.accounts:
        if account.type is not AccountType.CREDIT_CARD:
            continue
        txs = [t for t in plan.transactions if t.account_key == account.key]
        for pay in (t for t in txs if t.kind is TxKind.DEPOSIT):
            assert pay.day.day == 20 and pay.internal, pay
            cutoff = pay.day.replace(day=1)
            charged = sum(
                t.amount
                for t in txs
                if t.kind is TxKind.PURCHASE and t.status is TxStatus.COMPLETED and t.day < cutoff
            )
            paid_before = sum(t.amount for t in txs if t.kind is TxKind.DEPOSIT and t.day < pay.day)
            due = charged - paid_before
            assert 0 < pay.amount <= due, (account.key, pay, due)
            pairs.append((pay.amount, due))
    return pairs


def _reserve(persona: catalog.Persona) -> int:
    """Independent restatement of D48's reserve: a month of bills at their highest plus
    the savings transfer."""
    return sum(b.high for b in persona.bills) + persona.savings_transfer


def _assert_optional_spending_keeps_the_reserve(
    plan: SeedPlan, personas: tuple[catalog.Persona, ...]
) -> None:
    """After every optional checking debit (anything but a bill payment or an internal
    move), checking still holds the reserve, so rent is always there on the 1st."""
    paying = {b.paid_by_tx_key for b in plan.bills if b.paid_by_tx_key}
    for persona in personas:
        key = f"{persona.key}/checking"
        reserve = _reserve(persona)

        def fixed(t: PlanTx) -> bool:
            return t.internal or t.key in paying

        txs = sorted(
            (t for t in plan.transactions if t.account_key == key),
            # same order as the generator within a day: credits, fixed, optional
            key=lambda t: (t.day, 0 if t.kind is TxKind.DEPOSIT else 1 if fixed(t) else 2),
        )
        running = plan.account(key).opening_balance
        for tx in txs:
            running += tx.amount if tx.kind is TxKind.DEPOSIT else -tx.amount
            if tx.kind is not TxKind.DEPOSIT and not fixed(tx):
                assert running >= reserve, (plan.seed, tx, running, reserve)


def test_optional_spending_keeps_the_reserve(plan: SeedPlan) -> None:
    _assert_optional_spending_keeps_the_reserve(plan, catalog.PERSONAS)


def test_tight_budget_pays_the_card_partly_and_keeps_rent_money(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persona whose card spending outruns her income: the card is paid only down to
    the reserve, the rest stays owed, and fixed payments never overdraw checking."""
    base = catalog.PERSONAS[2]  # the traveller: big card spending
    tight = base.__class__(
        **{**base.__dict__, "income": (catalog.Income("Small job", 2_300, (3,)),)}
    )
    monkeypatch.setattr("ledgervoice.seed.generator.PERSONAS", (tight,))
    plan = generate(SEED, ANCHOR)
    pairs = _assert_card_payments_follow_the_statement(plan)
    assert any(paid < due for paid, due in pairs), "scenario must exercise partial payment"
    _assert_never_negative(plan)
    _assert_optional_spending_keeps_the_reserve(plan, (tight,))
    assert GroundTruth(plan).amount_owed(f"{tight.key}/card") > 0


def test_checking_and_savings_never_go_negative_on_any_day(plan: SeedPlan) -> None:
    _assert_never_negative(plan)


def test_card_payments_follow_the_statement(plan: SeedPlan) -> None:
    pairs = _assert_card_payments_follow_the_statement(plan)
    assert pairs and all(paid == due for paid, due in pairs)  # seed 42: always in full


@pytest.mark.parametrize("anchor_offset", [0, 37, 74, 111])
def test_many_seeds_and_dates_obey_the_money_rules(anchor_offset: int) -> None:
    """Seed 42 alone once hid a real bug (rent overdrawing checking for 13 of 240
    seed/date combinations), so the rules are checked across many plans."""
    anchor = ANCHOR + timedelta(days=anchor_offset)
    for seed in range(25):
        plan = generate(seed, anchor)  # raises SeedConfigError if rent can't be paid
        _assert_never_negative(plan)
        _assert_card_payments_follow_the_statement(plan)
        _assert_optional_spending_keeps_the_reserve(plan, catalog.PERSONAS)
        truth = GroundTruth(plan)
        for account in plan.accounts:
            if account.type is AccountType.CREDIT_CARD:
                assert truth.amount_owed(account.key) >= 0


def test_only_recent_discretionary_purchases_are_pending(plan: SeedPlan) -> None:
    pending = [t for t in plan.transactions if t.status is TxStatus.PENDING]
    assert pending, "the plan should exercise the completed-only balance rule"
    for tx in pending:
        assert tx.kind is TxKind.PURCHASE and not tx.internal
        assert (ANCHOR - tx.day).days <= 3


def test_completed_bills_are_paid_by_a_matching_purchase(plan: SeedPlan) -> None:
    by_key = {t.key: t for t in plan.transactions}
    completed = [b for b in plan.bills if b.status is BillStatus.COMPLETED]
    assert completed
    for bill in completed:
        assert bill.paid_by_tx_key is not None
        tx = by_key[bill.paid_by_tx_key]
        assert (tx.kind, tx.status) == (TxKind.PURCHASE, TxStatus.COMPLETED)
        assert (tx.account_key, tx.amount, tx.day) == (
            bill.account_key,
            bill.amount,
            bill.payment_date,
        )
        assert tx.description == bill.payee


def test_upcoming_bills_move_no_money(plan: SeedPlan) -> None:
    for bill in plan.bills:
        if bill.status is BillStatus.PENDING:
            assert bill.payment_date >= ANCHOR and bill.paid_by_tx_key is None
        if bill.status is BillStatus.CANCELLED:
            assert bill.paid_by_tx_key is None
    assert sum(b.status is BillStatus.CANCELLED for b in plan.bills) == 1
    # one upcoming bill per monthly bill in the catalog
    expected = sum(len(p.bills) for p in catalog.PERSONAS)
    assert sum(b.status is BillStatus.PENDING for b in plan.bills) == expected


def test_internal_moves_come_in_matching_pairs(plan: SeedPlan) -> None:
    """Savings transfers and card payments: a withdrawal from checking and a deposit
    to the customer's own savings or card, same day and amount."""
    legs: dict[tuple[str, date, int], list[PlanTx]] = defaultdict(list)
    for tx in plan.transactions:
        if tx.internal:
            customer = tx.account_key.split("/")[0]
            legs[(customer, tx.day, tx.amount)].append(tx)
    assert legs
    for group in legs.values():
        out = [t for t in group if t.kind is TxKind.WITHDRAWAL]
        into = [t for t in group if t.kind is TxKind.DEPOSIT]
        assert len(out) == len(into) and len(group) == 2 * len(out), group
        assert all(t.account_key.endswith("/checking") for t in out), group
        assert all(not t.account_key.endswith("/checking") for t in into), group
        assert all("LV-TRF-" not in t.description for t in group)  # not saga transfers


def test_card_payments_never_overpay(plan: SeedPlan, truth: GroundTruth) -> None:
    for account in plan.accounts:
        if account.type is AccountType.CREDIT_CARD:
            assert truth.amount_owed(account.key) >= 0


def _expected_monthly_net(persona: catalog.Persona) -> float:
    """Income minus expected spending, bills, ATM cash and the savings transfer, from the
    catalog's own numbers (expected values: average amount x expected frequency)."""
    weeks_per_month = 365.25 / 12 / 7
    income = sum(i.amount * len(i.days_of_month) for i in persona.income)
    outflow = (
        sum((b.low + b.high) / 2 for b in persona.bills)
        + sum(s.amount for s in persona.subscriptions)
        + sum(h.per_week * weeks_per_month * (h.low + h.high) / 2 for h in persona.habits)
        + persona.atm_per_month * sum(persona.atm_amounts) / len(persona.atm_amounts)
        + persona.savings_transfer
    )
    return income - outflow


@pytest.mark.parametrize("persona", catalog.PERSONAS, ids=lambda p: p.key)
def test_every_persona_can_afford_their_life(persona: catalog.Persona) -> None:
    """A persona who spends more than they earn makes unrealistic data (and once made
    the generator crash). Tuning the catalog must keep every budget at or above zero."""
    assert _expected_monthly_net(persona) >= 0, f"{persona.key} overspends on average"


def test_a_persona_that_cannot_pay_rent_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    broke = catalog.PERSONAS[0].__class__(
        **{**catalog.PERSONAS[0].__dict__, "opening_checking": 0, "income": ()}
    )
    monkeypatch.setattr("ledgervoice.seed.generator.PERSONAS", (broke,))
    with pytest.raises(SeedConfigError, match="would overdraw"):
        generate(SEED, ANCHOR)


# --- ground truth agrees with the bank ---------------------------------------------
def test_truth_matches_the_bank(plan: SeedPlan, truth: GroundTruth) -> None:
    bank, manifest = seed_fake(plan)  # the real applier (M1c-2)
    ids = {**manifest.merchants, **manifest.accounts}
    category_of = {m.id: m.category for m in bank.list_merchants()}
    for account in plan.accounts:
        computed = compute_balance(bank, ids[account.key]).current
        assert computed == truth.balance(account.key), account.key
        if account.type is AccountType.CREDIT_CARD:
            assert -computed == truth.amount_owed(account.key)
    full = (plan.start, ANCHOR - timedelta(days=1))
    for c in plan.customers:
        from_bank: dict[str, int] = defaultdict(int)
        for a in plan.accounts_of(c.key):
            for p in bank.list_purchases(ids[a.key]):
                if p.status is TxStatus.COMPLETED:
                    assert p.merchant_id is not None
                    category = category_of[p.merchant_id]
                    assert category is not None  # every seeded merchant has one
                    from_bank[category] += p.amount
        assert dict(sorted(from_bank.items())) == truth.spend_by_category(c.key, *full)
        checking = ids[f"{c.key}/checking"]
        statuses = sorted(b.status.value for b in bank.list_bills(checking))
        assert statuses == sorted(b.status.value for b in truth.bills(c.key))


# --- ground truth queries on a tiny hand-made plan ----------------------------------
def _tiny() -> GroundTruth:
    d = date(2026, 11, 10)
    plan = SeedPlan(
        seed=0,
        anchor=date(2026, 11, 20),
        start=date(2026, 8, 22),
        merchants=(
            PlanMerchant("m-food", "Food Co", "groceries"),
            PlanMerchant("m-cafe", "Cafe", "coffee"),
        ),
        customers=(PlanCustomer("u", "U", "Ser", "student"),),
        accounts=(
            PlanAccount("u/checking", "u", AccountType.CHECKING, "C", 100),
            PlanAccount("u/savings", "u", AccountType.SAVINGS, "S", 0),
            PlanAccount("u/card", "u", AccountType.CREDIT_CARD, "Card", 0),
        ),
        transactions=(
            PlanTx("t1", "u/checking", TxKind.DEPOSIT, 500, d, "Pay", TxStatus.COMPLETED),
            PlanTx(
                "t2", "u/checking", TxKind.PURCHASE, 40, d, "Food Co", TxStatus.COMPLETED, "m-food"
            ),
            PlanTx(
                "t3",
                "u/card",
                TxKind.PURCHASE,
                7,
                d + timedelta(1),
                "Cafe",
                TxStatus.COMPLETED,
                "m-cafe",
            ),
            PlanTx(
                "t4",
                "u/card",
                TxKind.PURCHASE,
                9,
                d + timedelta(8),
                "Cafe",
                TxStatus.PENDING,
                "m-cafe",
            ),
            PlanTx(
                "t5",
                "u/checking",
                TxKind.WITHDRAWAL,
                30,
                d + timedelta(2),
                "To savings",
                TxStatus.COMPLETED,
                internal=True,
            ),
            PlanTx(
                "t6",
                "u/savings",
                TxKind.DEPOSIT,
                30,
                d + timedelta(2),
                "From checking",
                TxStatus.COMPLETED,
                internal=True,
            ),
            PlanTx(
                "t7",
                "u/checking",
                TxKind.DEPOSIT,
                10,
                d + timedelta(2),
                "From savings",
                TxStatus.COMPLETED,
                internal=True,
            ),
            PlanTx(
                "t8",
                "u/savings",
                TxKind.WITHDRAWAL,
                10,
                d + timedelta(2),
                "To checking",
                TxStatus.COMPLETED,
                internal=True,
            ),
        ),
        bills=(),
    )
    return GroundTruth(plan)


def test_tiny_balances_and_owed() -> None:
    t = _tiny()
    assert t.balance("u/checking") == 100 + 500 - 40 - 30 + 10
    assert t.balance("u/savings") == 30 - 10
    assert t.amount_owed("u/card") == 7  # the pending 9 is not owed yet
    assert t.pending_debits("u/card") == 9
    with pytest.raises(ValueError):
        t.amount_owed("u/checking")


def test_tiny_spend_filters() -> None:
    t = _tiny()
    nov = (date(2026, 11, 1), date(2026, 11, 30))
    assert t.spend("u", *nov).total == 47
    assert t.spend("u", *nov, include_pending=True).total == 56
    assert t.spend("u", *nov, category="coffee").total == 7
    assert t.spend("u", *nov, merchant_key="m-food").count == 1
    assert t.spend("u", *nov, account_key="u/card").total == 7
    one_day = (date(2026, 11, 10), date(2026, 11, 10))
    assert t.spend("u", *one_day).total == 40  # both ends inclusive
    with pytest.raises(ValueError):
        t.spend("u", *nov, account_key="someone/checking")


def test_tiny_income_ignores_internal_moves() -> None:
    t = _tiny()
    assert t.income("u", date(2026, 11, 1), date(2026, 11, 30)) == 500
    assert t.last_n_days(7) == (date(2026, 11, 13), date(2026, 11, 19))


# --- command line --------------------------------------------------------------------
def test_cli_writes_plan_and_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = seed_cli(
        ["plan", "--seed", str(SEED), "--anchor", ANCHOR.isoformat(), "--out", str(tmp_path)]
    )
    assert rc == 0
    assert GOLDEN_DIGEST in capsys.readouterr().out
    written = json.loads((tmp_path / "plan.json").read_text())
    assert written["seed"] == SEED and written["anchor"] == ANCHOR.isoformat()
    summary = json.loads((tmp_path / "truth-summary.json").read_text())
    assert summary["plan_digest"] == GOLDEN_DIGEST
    assert set(summary["customers"]) == {"priya", "marcus", "elena"}
