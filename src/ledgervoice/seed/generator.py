"""Deterministic seed generator: (seed, anchor date) -> SeedPlan. No network, no clock.

Money-flow rules (D48), chosen so the ground truth equals what compute_balance (D18)
reports once the plan is in the bank:
- Opening balances are the accounts' stored Nessie balances at the start of the window.
- Income arrives as completed deposits to checking.
- A paid bill is a `completed` bill plus a purchase at the biller on the same date for
  the same amount (Nessie bills never move money). The next bill after the anchor date
  is `pending` and moves nothing. One persona has a `cancelled` bill.
- Subscriptions are monthly card purchases.
- On the 20th the card is paid: a withdrawal from checking plus a deposit to the card.
  It pays the statement (completed card purchases before the 1st of that month, minus
  earlier payments) in full when checking can cover it without touching the reserve,
  otherwise as much as it can; the rest stays owed.
- On the 25th a fixed amount moves checking -> savings (a withdrawal plus a deposit).
  These carry no LV-TRF tag: they are history, not transfers made by our saga.
- Discretionary purchases in the last 3 days are `pending` about half the time.
- Reserve: a month of bills plus the savings transfer. Discretionary debit spending
  that would take checking below the reserve is dropped (pending debits count), so
  rent is always there on the 1st. Fixed payments may use the reserve; one that would
  overdraw checking is a persona configuration error and raises SeedConfigError.

Randomness: one random.Random per persona, seeded with a string built from the seed and
the persona key (string seeds are deterministic; the built-in hash() is never used).
"""

from __future__ import annotations

import calendar
import random
from dataclasses import dataclass
from datetime import date, timedelta

from nessie_tools import AccountType, BillStatus, TxStatus

from ledgervoice.seed.catalog import MERCHANTS, PERSONAS, Persona
from ledgervoice.seed.plan import (
    PlanAccount,
    PlanBill,
    PlanCustomer,
    PlanMerchant,
    PlanTx,
    SeedPlan,
    TxKind,
)

HISTORY_DAYS = 90
PENDING_WINDOW_DAYS = 3
CARD_PAYMENT_DAY = 20
SAVINGS_DAY = 25

_CREDIT, _FIXED, _OPTIONAL = 0, 1, 2  # order of processing within a day


@dataclass
class _Draft:
    ident: int
    day: date
    role: str  # "checking" | "savings" | "card"
    kind: TxKind
    amount: int
    description: str
    priority: int
    merchant: str | None = None
    status: TxStatus = TxStatus.COMPLETED
    internal: bool = False


@dataclass
class _DraftBill:
    merchant: str
    nickname: str
    amount: int
    payment_date: date
    recurring_day: int
    status: BillStatus
    paid_by: int | None = None  # ident of the paying purchase draft


class SeedConfigError(ValueError):
    """The persona catalog makes the plan impossible (e.g. rent would overdraw)."""


def generate(seed: int, anchor: date) -> SeedPlan:
    start = anchor - timedelta(days=HISTORY_DAYS)
    merchants = tuple(
        PlanMerchant(key=k, name=name, category=cat) for k, (name, cat) in sorted(MERCHANTS.items())
    )
    customers: list[PlanCustomer] = []
    accounts: list[PlanAccount] = []
    transactions: list[PlanTx] = []
    bills: list[PlanBill] = []
    for persona in PERSONAS:
        c, a, t, b = _persona_plan(persona, seed, start, anchor)
        customers.append(c)
        accounts.extend(a)
        transactions.extend(t)
        bills.extend(b)
    return SeedPlan(
        seed=seed,
        anchor=anchor,
        start=start,
        merchants=merchants,
        customers=tuple(customers),
        accounts=tuple(accounts),
        transactions=tuple(transactions),
        bills=tuple(bills),
    )


# --- one persona -----------------------------------------------------------------
def _persona_plan(
    p: Persona, seed: int, start: date, anchor: date
) -> tuple[PlanCustomer, list[PlanAccount], list[PlanTx], list[PlanBill]]:
    rng = random.Random(f"ledgervoice-seed:{seed}:{p.key}")
    last = anchor - timedelta(days=1)
    days = [start + timedelta(days=i) for i in range(HISTORY_DAYS)]
    counter = iter(range(1_000_000))
    drafts: list[_Draft] = []
    draft_bills: list[_DraftBill] = []

    def add(
        day: date,
        role: str,
        kind: TxKind,
        amount: int,
        desc: str,
        prio: int,
        merchant: str | None = None,
    ) -> _Draft:
        d = _Draft(next(counter), day, role, kind, amount, desc, prio, merchant)
        drafts.append(d)
        return d

    months = _months_between(start, anchor + timedelta(days=31))

    # income
    for inc in p.income:
        for y, m in months:
            for dom in inc.days_of_month:
                day = _clamp_day(y, m, dom)
                if start <= day <= last:
                    add(day, "checking", TxKind.DEPOSIT, inc.amount, inc.description, _CREDIT)

    # bills: paid ones in the window, the next one pending
    for bill in p.bills:
        name = MERCHANTS[bill.merchant][0]
        upcoming_done = False
        for y, m in months:
            due = _clamp_day(y, m, bill.day)
            amount = rng.randint(bill.low, bill.high)
            if start <= due <= last:
                paying = add(due, "checking", TxKind.PURCHASE, amount, name, _FIXED, bill.merchant)
                draft_bills.append(
                    _DraftBill(
                        bill.merchant,
                        bill.nickname,
                        amount,
                        due,
                        bill.day,
                        BillStatus.COMPLETED,
                        paying.ident,
                    )
                )
            elif due >= anchor and not upcoming_done:
                draft_bills.append(
                    _DraftBill(
                        bill.merchant, bill.nickname, amount, due, bill.day, BillStatus.PENDING
                    )
                )
                upcoming_done = True
    if p.cancelled_bill is not None:
        merchant, nickname, amount, day_of_month = p.cancelled_bill
        draft_bills.append(
            _DraftBill(
                merchant,
                nickname,
                amount,
                _first_on_or_after(start, day_of_month),
                day_of_month,
                BillStatus.CANCELLED,
            )
        )

    # subscriptions on the card
    for sub in p.subscriptions:
        for y, m in months:
            day = _clamp_day(y, m, sub.day)
            if start <= day <= last:
                add(
                    day,
                    "card",
                    TxKind.PURCHASE,
                    sub.amount,
                    MERCHANTS[sub.merchant][0],
                    _FIXED,
                    sub.merchant,
                )

    # savings transfers
    for y, m in months:
        day = _clamp_day(y, m, SAVINGS_DAY)
        if start <= day <= last:
            add(
                day,
                "checking",
                TxKind.WITHDRAWAL,
                p.savings_transfer,
                "Transfer to savings",
                _FIXED,
            ).internal = True
            add(
                day,
                "savings",
                TxKind.DEPOSIT,
                p.savings_transfer,
                "Transfer from checking",
                _CREDIT,
            ).internal = True

    # ATM cash
    for y, m in months:
        in_month = [d for d in days if (d.year, d.month) == (y, m)]
        if not in_month:
            continue
        whole, frac = divmod(p.atm_per_month, 1.0)
        n = int(whole) + (1 if rng.random() < frac else 0)
        for _ in range(n):
            day = rng.choice(in_month)
            add(
                day,
                "checking",
                TxKind.WITHDRAWAL,
                rng.choice(p.atm_amounts),
                "ATM withdrawal",
                _OPTIONAL,
            )

    # everyday spending
    for day in days:
        for habit in p.habits:
            if rng.random() >= habit.per_week / 7:
                continue
            merchant = rng.choice(habit.merchants)
            amount = rng.randint(habit.low, habit.high)
            role = "card" if rng.random() < habit.card_share else "checking"
            d = add(day, role, TxKind.PURCHASE, amount, MERCHANTS[merchant][0], _OPTIONAL, merchant)
            if (last - day).days < PENDING_WINDOW_DAYS and rng.random() < 0.5:
                d.status = TxStatus.PENDING

    accepted = _replay(p, drafts, days)
    return _finish(p, accepted, draft_bills)


def _reserve(p: Persona) -> int:
    """Money a person keeps back for fixed costs: a month of bills (at their highest)
    plus the savings transfer. Optional spending never dips into it."""
    return sum(b.high for b in p.bills) + p.savings_transfer


def _replay(p: Persona, drafts: list[_Draft], days: list[date]) -> list[_Draft]:
    """Walk the days in order. Optional checking debits must leave the reserve intact;
    the card is paid on the 20th, in full if that leaves the reserve intact, otherwise
    as much as possible (the rest stays owed). Fixed debits may use the reserve but
    never overdraw checking."""
    by_day: dict[date, list[_Draft]] = {}
    for d in drafts:
        by_day.setdefault(d.day, []).append(d)
    reserve = _reserve(p)
    checking = p.opening_checking
    accepted: list[_Draft] = []
    next_ident = max((d.ident for d in drafts), default=0) + 1

    for day in days:
        todays = sorted(by_day.get(day, []), key=lambda d: (d.priority, d.ident))
        if day.day == CARD_PAYMENT_DAY:
            pay = min(_statement_due(accepted, day.replace(day=1)), max(0, checking - reserve))
            if pay > 0:
                todays += [
                    _Draft(
                        next_ident,
                        day,
                        "checking",
                        TxKind.WITHDRAWAL,
                        pay,
                        "Credit card payment",
                        _FIXED,
                        internal=True,
                    ),
                    _Draft(
                        next_ident + 1,
                        day,
                        "card",
                        TxKind.DEPOSIT,
                        pay,
                        "Payment received - thank you",
                        _CREDIT,
                        internal=True,
                    ),
                ]
                next_ident += 2
                todays.sort(key=lambda d: (d.priority, d.ident))
        for d in todays:
            if d.role == "checking":
                delta = d.amount if d.kind is TxKind.DEPOSIT else -d.amount
                if d.priority == _OPTIONAL and checking + delta < reserve:
                    continue  # would eat into rent money: it didn't happen
                if checking + delta < 0:
                    raise SeedConfigError(
                        f"{p.key}: {d.description} ${d.amount} on {d.day} would overdraw "
                        f"checking (balance ${checking}); raise income or opening_checking"
                    )
                checking += delta
            accepted.append(d)
    return accepted


def _statement_due(accepted: list[_Draft], cutoff: date) -> int:
    """What the card statement asks for: completed card purchases made before `cutoff`
    (the 1st of the payment month), minus every payment made so far."""
    charged = sum(
        d.amount
        for d in accepted
        if d.role == "card"
        and d.kind is TxKind.PURCHASE
        and d.status is TxStatus.COMPLETED
        and d.day < cutoff
    )
    paid = sum(d.amount for d in accepted if d.role == "card" and d.kind is TxKind.DEPOSIT)
    return max(0, charged - paid)


def _finish(
    p: Persona, accepted: list[_Draft], draft_bills: list[_DraftBill]
) -> tuple[PlanCustomer, list[PlanAccount], list[PlanTx], list[PlanBill]]:
    customer = PlanCustomer(p.key, p.first_name, p.last_name, persona=p.kind)
    accounts = [
        PlanAccount(
            f"{p.key}/checking",
            p.key,
            AccountType.CHECKING,
            "Everyday Checking",
            p.opening_checking,
        ),
        PlanAccount(f"{p.key}/savings", p.key, AccountType.SAVINGS, "Savings", p.opening_savings),
        PlanAccount(f"{p.key}/card", p.key, AccountType.CREDIT_CARD, "Rewards Card", 0),
    ]
    role_order = {"checking": 0, "savings": 1, "card": 2}
    ordered = sorted(accepted, key=lambda d: (d.day, role_order[d.role], d.ident))
    key_of = {d.ident: f"{p.key}/tx/{i:04d}" for i, d in enumerate(ordered, start=1)}
    txs = [
        PlanTx(
            key=key_of[d.ident],
            account_key=f"{p.key}/{d.role}",
            kind=d.kind,
            amount=d.amount,
            day=d.day,
            description=d.description,
            status=d.status,
            merchant_key=d.merchant,
            internal=d.internal,
        )
        for d in ordered
    ]
    bills_sorted = sorted(draft_bills, key=lambda b: (b.payment_date, b.merchant, b.nickname))
    bills = [
        PlanBill(
            key=f"{p.key}/bill/{i:02d}",
            account_key=f"{p.key}/checking",
            payee=MERCHANTS[b.merchant][0],
            nickname=b.nickname,
            amount=b.amount,
            payment_date=b.payment_date,
            recurring_day=b.recurring_day,
            status=b.status,
            paid_by_tx_key=key_of[b.paid_by] if b.paid_by is not None else None,
        )
        for i, b in enumerate(bills_sorted, start=1)
    ]
    return customer, accounts, txs, bills


# --- calendar helpers --------------------------------------------------------------
def _months_between(first: date, last: date) -> list[tuple[int, int]]:
    months: list[tuple[int, int]] = []
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


def _first_on_or_after(first: date, day: int) -> date:
    """The first date on or after `first` that falls on `day` of its month (clamped)."""
    this_month = _clamp_day(first.year, first.month, day)
    if this_month >= first:
        return this_month
    y, m = (first.year + 1, 1) if first.month == 12 else (first.year, first.month + 1)
    return _clamp_day(y, m, day)


def _clamp_day(year: int, month: int, day: int) -> date:
    """`day` of the month, or the month's last day if it is shorter."""
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))
