"""Write a seed plan into a bank (M1c-2).

Order: merchants -> customers -> accounts -> transactions -> bills. Merchants and
customers are reused from the registry when the bank still has them with the same
name (and category, for merchants); otherwise new ones are created (D51). Accounts,
transactions and bills are always new: reset deletes the accounts, and Nessie keeps
their transactions as orphans (D36), so a reseed must never reuse an account.

Any error aborts the run (D42): there are no retries on writes. The manifest is saved
after every write (`save`), so an aborted run still lists every account it created and
reset can clean up. Nothing here reads the clock except the timing and the two
timestamps in the manifest, so the bank contents depend only on the plan.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TypeVar

from nessie_tools import (
    Address,
    AuditLog,
    BankBackend,
    FakeBackend,
    NotFoundError,
)

from ledgervoice.seed.manifest import Manifest, Registry, SeedStatus
from ledgervoice.seed.plan import SeedPlan, TxKind

SEED_ADDRESS = Address(
    street_number="100", street_name="Ledger Lane", city="Springfield", state="VA", zip="22150"
)
FAKE_SERVER = "fake"
T = TypeVar("T")


class SeedError(RuntimeError):
    """A seed operation was refused or could not finish; the message says what to do."""


def apply_plan(
    plan: SeedPlan,
    backend: BankBackend,
    *,
    server: str,
    registry: Registry,
    save: Callable[[Manifest], None] = lambda _: None,
    save_registry: Callable[[Registry], None] = lambda _: None,
    audit_dir: Path | None = None,
    on_progress: Callable[[int, int], None] = lambda done, total: None,
    clock: Callable[[], float] = time.perf_counter,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Manifest:
    manifest = Manifest(
        server=server,
        seed=plan.seed,
        anchor=plan.anchor,
        digest=plan.digest(),
        started_at=now().isoformat(timespec="seconds"),
    )
    save(manifest)
    total = (
        len(plan.merchants)
        + len(plan.customers)
        + len(plan.accounts)
        + len(plan.transactions)
        + len(plan.bills)
    )
    durations: list[float] = []
    done = 0

    def timed(call: Callable[[], T]) -> T:
        nonlocal done
        start = clock()
        result = call()
        durations.append(clock() - start)
        done += 1
        on_progress(done, total)
        return result

    # merchants: reuse when the bank still has the same name and category
    for m in plan.merchants:
        known = registry.get(server, "merchants", m.key)
        if known is not None and _merchant_matches(backend, known, m.name, m.category):
            manifest.merchants[m.key] = known
            done += 1
            on_progress(done, total)
        else:
            created = timed(partial(backend.create_merchant, m.name, m.category))
            manifest.merchants[m.key] = created.id
            registry.set(server, "merchants", m.key, created.id)
            save_registry(registry)
        save(manifest)

    # customers: reuse when the bank still has the same name
    for c in plan.customers:
        known = registry.get(server, "customers", c.key)
        if known is not None and _customer_matches(backend, known, c.first_name, c.last_name):
            manifest.customers[c.key] = known
            done += 1
            on_progress(done, total)
        else:
            created_c = timed(
                partial(backend.create_customer, c.first_name, c.last_name, SEED_ADDRESS)
            )
            manifest.customers[c.key] = created_c.id
            registry.set(server, "customers", c.key, created_c.id)
            save_registry(registry)
        save(manifest)

    # accounts: always new
    for a in plan.accounts:
        customer_id = manifest.customers[a.customer_key]
        account = timed(
            partial(backend.create_account, customer_id, a.type, a.nickname, a.opening_balance)
        )
        manifest.accounts[a.key] = account.id
        save(manifest)

    # transactions
    for tx in plan.transactions:
        account_id = manifest.accounts[tx.account_key]
        if tx.kind is TxKind.PURCHASE:
            if tx.merchant_key is None:  # the generator never does this (tested)
                raise SeedError(f"{tx.key}: purchase without a merchant")
            merchant_id = manifest.merchants[tx.merchant_key]
            record_id = timed(
                partial(
                    backend.create_purchase,
                    account_id,
                    merchant_id,
                    tx.amount,
                    tx.day,
                    tx.description,
                    tx.status,
                )
            ).id
        elif tx.kind is TxKind.DEPOSIT:
            record_id = timed(
                partial(
                    backend.create_deposit,
                    account_id,
                    tx.amount,
                    tx.day,
                    tx.description,
                    tx.status,
                )
            ).id
        else:
            record_id = timed(
                partial(
                    backend.create_withdrawal,
                    account_id,
                    tx.amount,
                    tx.day,
                    tx.description,
                    tx.status,
                )
            ).id
        manifest.transactions[tx.key] = record_id
        save(manifest)

    # bills
    for b in plan.bills:
        account_id = manifest.accounts[b.account_key]
        bill = timed(
            partial(
                backend.create_bill,
                account_id,
                b.payee,
                b.nickname,
                b.amount,
                b.payment_date,
                b.recurring_day,
                status=b.status,
            )
        )
        manifest.bills[b.key] = bill.id
        save(manifest)

    if audit_dir is not None:  # a fresh, empty audit database for this seed (R1-07)
        stamp = manifest.started_at.replace(":", "").replace("-", "")
        path = audit_dir / f"seed-{manifest.digest[:8]}-{stamp}.sqlite"
        AuditLog(path).close()
        manifest.audit_db = str(path)
    manifest.timing = _timing(durations)
    manifest.status = SeedStatus.COMPLETE
    manifest.finished_at = now().isoformat(timespec="seconds")
    save(manifest)
    return manifest


def seed_fake(plan: SeedPlan, *, fake_seed: int = 0) -> tuple[FakeBackend, Manifest]:
    """An in-memory bank holding exactly the plan (for tests and the M1f evals; D51).
    The fake lives only as long as the process, so there is no file and no reset."""
    backend = FakeBackend(seed=fake_seed)
    manifest = apply_plan(plan, backend, server=FAKE_SERVER, registry=Registry())
    return backend, manifest


def _merchant_matches(backend: BankBackend, merchant_id: str, name: str, category: str) -> bool:
    try:
        found = backend.get_merchant(merchant_id)
    except NotFoundError:
        return False
    return found.name == name and found.category == category


def _customer_matches(backend: BankBackend, customer_id: str, first: str, last: str) -> bool:
    try:
        found = backend.get_customer(customer_id)
    except NotFoundError:
        return False
    return (found.first_name, found.last_name) == (first, last)


def _timing(durations: list[float]) -> dict[str, float]:
    """Write latency summary, in milliseconds, for the run log."""
    if not durations:
        return {"writes": 0}
    ordered = sorted(durations)

    def pct(p: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(p * len(ordered)))] * 1000, 1)

    return {
        "writes": len(ordered),
        "total_s": round(sum(ordered), 1),
        "p50_ms": pct(0.50),
        "p95_ms": pct(0.95),
        "max_ms": round(ordered[-1] * 1000, 1),
    }
