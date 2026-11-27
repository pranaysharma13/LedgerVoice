"""Read-back verification: prove the bank holds exactly the plan (M1c-2, D24, D43, D47).

Every planned record is fetched through the manifest's ids and compared field by
field. Anything else found in a seeded account is reported as unexpected, and so is
any account under a seed customer that the manifest doesn't list (R2-06: e.g. one
created by a write that timed out). Finally compute_balance (what the agent will
see) must equal the ground truth for every account, and reconcile on the seed's
fresh audit database must be clean.

All problems are collected and returned together; nothing stops at the first one. A
read that fails with a temporary error is retried once (D41). A record or account
the bank says is missing, an id the manifest lacks, and a read that fails for good
(e.g. a 400 or malformed data) are all problems, not crashes (R2-16, R2-20).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from functools import partial
from typing import TypeVar

from nessie_tools import (
    Account,
    AuditLog,
    BankBackend,
    BankError,
    NotFoundError,
    TxStatus,
    compute_balance,
    is_temporary,
    reconcile,
)

from ledgervoice.seed.manifest import Manifest, SeedStatus
from ledgervoice.seed.plan import PlanAccount, SeedPlan, TxKind
from ledgervoice.seed.truth import GroundTruth

T = TypeVar("T")


@dataclass
class VerifyReport:
    problems: list[str] = field(default_factory=list)
    checked: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems

    def count(self, kind: str, n: int = 1) -> None:
        self.checked[kind] = self.checked.get(kind, 0) + n


def verify(
    plan: SeedPlan,
    backend: BankBackend,
    manifest: Manifest,
    *,
    audit: AuditLog | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> VerifyReport:
    report = VerifyReport()
    problem = report.problems.append

    def read(call: Callable[[], T]) -> T:
        return _read(call, sleep)

    if manifest.digest != plan.digest():
        problem(
            "the manifest was written for a different plan (seed, date or catalog changed); "
            "reset and reseed"
        )
        return report
    if manifest.status is not SeedStatus.COMPLETE:
        problem(f"the seed is {manifest.status.value}, not complete; reset and reseed")
        return report

    # merchants and customers
    for m in plan.merchants:
        mid = manifest.merchants.get(m.key)
        if not mid:
            problem(f"merchant {m.key}: not in the manifest")
            continue
        try:
            found = read(partial(backend.get_merchant, mid))
        except NotFoundError:
            problem(f"merchant {m.key}: missing")
            continue
        except BankError as exc:
            problem(f"merchant {m.key}: could not be read: {exc}")
            continue
        if (found.name, found.category) != (m.name, m.category):
            problem(
                f"merchant {m.key}: is {found.name!r}/{found.category!r}, "
                f"planned {m.name!r}/{m.category!r}"
            )
        report.count("merchants")
    planned_accounts = {
        c.key: {manifest.accounts.get(a.key) for a in plan.accounts if a.customer_key == c.key}
        for c in plan.customers
    }
    for c in plan.customers:
        cid = manifest.customers.get(c.key)
        if not cid:
            problem(f"customer {c.key}: not in the manifest")
            continue
        try:
            found_c = read(partial(backend.get_customer, cid))
            listed = read(partial(backend.list_accounts, cid))
        except NotFoundError:
            problem(f"customer {c.key}: missing")
            continue
        except BankError as exc:
            problem(f"customer {c.key}: could not be read: {exc}")
            continue
        if (found_c.first_name, found_c.last_name) != (c.first_name, c.last_name):
            problem(f"customer {c.key}: name differs")
        for extra in sorted(acc.id for acc in listed if acc.id not in planned_accounts[c.key]):
            problem(f"customer {c.key}: account {extra} is not part of this seed (R2-06)")
        report.count("customers")

    # accounts, their records, and balances
    truth = GroundTruth(plan)
    for a in plan.accounts:
        aid = manifest.accounts.get(a.key)
        if not aid:
            problem(f"account {a.key}: not in the manifest")
            continue
        try:
            account = read(partial(backend.get_account, aid))
        except NotFoundError:
            problem(f"account {a.key}: missing at the bank")
            continue
        except BankError as exc:
            problem(f"account {a.key}: could not be read: {exc}")
            continue
        try:
            _check_account(plan, backend, manifest, a, aid, account, truth, sleep, report)
        except BankError as exc:  # R2-16: e.g. a bill list answering 400 (D49)
            problem(f"account {a.key}: a read failed ({exc}); its remaining checks were skipped")
    # the seed must start reconcile-clean (fresh audit DB, no saga legs)
    if audit is not None and not report.problems:
        try:
            rec = reconcile(
                backend,
                audit,
                list(manifest.accounts.values()),
                customer_ids=manifest.customer_ids(),
            )
        except BankError as exc:
            problem(f"reconcile could not finish: {exc}")
            return report
        for v in rec.violations:
            problem(f"reconcile: {v.kind.value}: {v.detail}")
        report.count("reconcile")
    return report


def _check_account(
    plan: SeedPlan,
    backend: BankBackend,
    manifest: Manifest,
    a: PlanAccount,
    aid: str,
    account: Account,
    truth: GroundTruth,
    sleep: Callable[[float], None],
    report: VerifyReport,
) -> None:
    """Fields, records, bills and balance of one seeded account. A read error that
    survives the retry propagates; the caller records it as a problem."""

    def read(call: Callable[[], T]) -> T:
        return _read(call, sleep)

    merchant_by_id = {v: k for k, v in manifest.merchants.items()}
    expected_owner = manifest.customers.get(a.customer_key)
    if (account.type, account.nickname, account.balance, account.customer_id) != (
        a.type,
        a.nickname,
        a.opening_balance,
        expected_owner,
    ):
        report.problems.append(f"account {a.key}: type/nickname/opening balance/owner differ")
    report.count("accounts")

    planned = [t for t in plan.transactions if t.account_key == a.key]
    for kind in TxKind:
        rows = read(partial(_rows, backend, kind, aid))
        expected_ids: set[str] = set()
        for tx in (t for t in planned if t.kind is kind):
            rid = manifest.transactions.get(tx.key, "")
            expected_ids.add(rid)
            row = rows.get(rid)
            if row is None:
                report.problems.append(f"{tx.key}: missing from {a.key}")
                continue
            if row[:4] != (tx.amount, tx.day, tx.description, tx.status):
                report.problems.append(
                    f"{tx.key}: bank has {row[0]}/{row[1]}/{row[2]!r}/{row[3]}, "
                    f"planned {tx.amount}/{tx.day}/{tx.description!r}/{tx.status}"
                )
            if kind is TxKind.PURCHASE and merchant_by_id.get(row[4] or "") != tx.merchant_key:
                report.problems.append(f"{tx.key}: merchant differs or is unknown (D47)")
            report.count("transactions")
        for extra in sorted(set(rows) - expected_ids):
            report.problems.append(f"{a.key}: unexpected {kind.value} {extra}")

    planned_bills = [b for b in plan.bills if b.account_key == a.key]
    listed_bills = {b.id: b for b in read(partial(backend.list_bills, aid))}
    expected_bill_ids = set()
    for pb in planned_bills:
        bid = manifest.bills.get(pb.key)
        if not bid:
            report.problems.append(f"{pb.key}: not in the manifest")
            continue
        expected_bill_ids.add(bid)
        bill = listed_bills.get(bid)
        if bill is None:
            report.problems.append(f"{pb.key}: missing from {a.key}")
            continue
        if (
            bill.payee,
            bill.nickname,
            bill.payment_amount,
            bill.payment_date,
            bill.recurring_date,
            bill.status,
        ) != (pb.payee, pb.nickname, pb.amount, pb.payment_date, pb.recurring_day, pb.status):
            report.problems.append(f"{pb.key}: fields differ")
        report.count("bills")
    for extra in sorted(set(listed_bills) - expected_bill_ids):
        report.problems.append(f"{a.key}: unexpected bill {extra}")

    balance = read(partial(compute_balance, backend, aid, account=account))
    if balance.current != truth.balance(a.key):
        report.problems.append(
            f"{a.key}: balance {balance.current} from the bank, "
            f"{truth.balance(a.key)} in the ground truth"
        )
    report.count("balances")


def _read[R](call: Callable[[], R], sleep: Callable[[float], None]) -> R:
    try:
        return call()
    except BankError as exc:
        if not is_temporary(exc):
            raise
        sleep(0.5)
        return call()  # one retry; a second failure propagates


Row = tuple[int, date | None, str, TxStatus | None, str | None]


def _rows(backend: BankBackend, kind: TxKind, account_id: str) -> dict[str, Row]:
    """id -> (amount, date, description, status, merchant id) for one record type."""
    if kind is TxKind.PURCHASE:
        return {
            p.id: (p.amount, p.purchase_date, p.description, p.status, p.merchant_id)
            for p in backend.list_purchases(account_id)
        }
    if kind is TxKind.DEPOSIT:
        return {
            d.id: (d.amount, d.transaction_date, d.description, d.status, None)
            for d in backend.list_deposits(account_id)
        }
    return {
        w.id: (w.amount, w.transaction_date, w.description, w.status, None)
        for w in backend.list_withdrawals(account_id)
    }
