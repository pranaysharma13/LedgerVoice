"""M1c-2 offline: applier, manifest, verification, reset and the CLI, on the fake bank."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from nessie_tools import (
    AccountType,
    AuditLog,
    BackendFailure,
    FakeBackend,
    NessieError,
    NotFoundError,
    TxStatus,
)

from ledgervoice.seed import generate
from ledgervoice.seed.__main__ import main as seed_cli
from ledgervoice.seed.apply import SeedError, apply_plan, seed_fake
from ledgervoice.seed.manifest import Manifest, Registry, SeedStatus, SeedStore
from ledgervoice.seed.plan import SeedPlan
from ledgervoice.seed.reset import reset
from ledgervoice.seed.truth import GroundTruth
from ledgervoice.seed.verify import verify

ANCHOR = date(2026, 11, 23)


@pytest.fixture(scope="module")
def plan() -> SeedPlan:
    return generate(42, ANCHOR)


@pytest.fixture
def small(plan: SeedPlan) -> SeedPlan:
    return plan.subset("priya", 15)


# --- apply + verify -----------------------------------------------------------------
def test_full_seed_verifies_cleanly(plan: SeedPlan) -> None:
    bank, manifest = seed_fake(plan)
    assert manifest.status is SeedStatus.COMPLETE
    assert len(manifest.transactions) == len(plan.transactions)
    report = verify(plan, bank, manifest, audit=AuditLog(":memory:"))
    assert report.ok, report.problems[:5]
    assert report.checked["transactions"] == len(plan.transactions)
    assert report.checked["bills"] == len(plan.bills)
    assert report.checked["balances"] == len(plan.accounts)
    assert report.checked["reconcile"] == 1


def test_reseeding_gives_identical_truth_with_new_ids(plan: SeedPlan) -> None:
    """DoD: a reseed holds the same data (same plan, same balances), only the ids differ."""
    bank_a, a = seed_fake(plan)
    bank_b, b = seed_fake(generate(42, ANCHOR), fake_seed=99)
    assert a.digest == b.digest
    assert set(a.accounts.values()).isdisjoint(b.accounts.values())
    truth = GroundTruth(plan)
    for key in a.accounts:
        from nessie_tools import compute_balance

        assert compute_balance(bank_a, a.accounts[key]).current == truth.balance(key)
        assert compute_balance(bank_b, b.accounts[key]).current == truth.balance(key)


def test_manifest_is_saved_after_every_write(small: SeedPlan) -> None:
    saved: list[str] = []
    apply_plan(
        small,
        FakeBackend(seed=1),
        server="fake",
        registry=Registry(),
        save=lambda m: saved.append(m.to_json()),
    )
    writes = len(small.merchants) + 1 + len(small.accounts) + len(small.transactions)
    writes += len(small.bills)
    assert len(saved) >= writes + 1
    first, last = Manifest.from_json(saved[0]), Manifest.from_json(saved[-1])
    assert first.status is SeedStatus.PARTIAL and not first.accounts
    assert last.status is SeedStatus.COMPLETE


def test_timing_is_recorded(small: SeedPlan) -> None:
    ticks = iter(range(10_000))
    manifest = apply_plan(
        small,
        FakeBackend(seed=1),
        server="fake",
        registry=Registry(),
        clock=lambda: next(ticks) * 0.1,
    )
    assert manifest.timing["writes"] == 33
    assert manifest.timing["p50_ms"] == pytest.approx(100.0)


# --- aborts and reset (D42) --------------------------------------------------------------
def test_an_aborted_seed_can_be_reset_and_reseeded(small: SeedPlan) -> None:
    bank = FakeBackend(seed=1)
    saved: list[Manifest] = []
    bank.fail_next("create_purchase")  # a clean rejection part-way through
    with pytest.raises(BackendFailure):
        apply_plan(
            small,
            bank,
            server="fake",
            registry=Registry(),
            save=lambda m: saved.append(Manifest.from_json(m.to_json())),
        )
    partial = saved[-1]
    assert partial.status is SeedStatus.PARTIAL
    assert len(partial.accounts) == 3  # everything reset needs to delete is listed
    assert reset(bank, partial, server="fake") == []
    for account_id in partial.accounts.values():
        with pytest.raises(NotFoundError):
            bank.get_account(account_id)
    again = apply_plan(small, bank, server="fake", registry=Registry())
    assert verify(small, bank, again).ok


def test_reset_deletes_only_the_manifest_accounts(small: SeedPlan) -> None:
    bank, manifest = seed_fake(small)
    bystander_customer = bank.create_customer("Not", "Ours", _address())
    bystander = bank.create_account(bystander_customer.id, AccountType.CHECKING, "Not ours", 5)
    assert reset(bank, manifest, server="fake") == []
    assert bank.get_account(bystander.id).nickname == "Not ours"
    assert manifest.status is SeedStatus.RESET and manifest.reset_at


def test_reset_refuses_a_manifest_from_another_server(small: SeedPlan) -> None:
    bank, manifest = seed_fake(small)
    with pytest.raises(SeedError, match="refusing"):
        reset(bank, manifest, server="https://api.nessieisreal.com")
    for account_id in manifest.accounts.values():
        bank.get_account(account_id)  # all still there
    assert manifest.status is SeedStatus.COMPLETE


def test_reset_tolerates_accounts_already_gone(small: SeedPlan) -> None:
    bank, manifest = seed_fake(small)
    bank.delete_account(manifest.accounts["priya/savings"])
    assert reset(bank, manifest, server="fake") == []
    assert manifest.status is SeedStatus.RESET


def test_a_failed_delete_leaves_the_manifest_for_a_retry(small: SeedPlan) -> None:
    bank, manifest = seed_fake(small)
    bank.fail_next("delete_account")
    problems = reset(bank, manifest, server="fake")
    assert len(problems) == 1 and "could not delete" in problems[0]
    assert manifest.status is SeedStatus.COMPLETE  # not marked reset
    assert reset(bank, manifest, server="fake") == []  # running it again finishes
    assert SeedStatus(manifest.status) is SeedStatus.RESET  # (re-read: reset mutates it)


def test_reset_archives_the_audit_database(small: SeedPlan, tmp_path: Path) -> None:
    bank = FakeBackend(seed=1)
    manifest = apply_plan(small, bank, server="fake", registry=Registry(), audit_dir=tmp_path / "a")
    audit = Path(str(manifest.audit_db))
    assert audit.exists()
    assert reset(bank, manifest, server="fake", archive_dir=tmp_path / "archive") == []
    assert not audit.exists()
    assert (tmp_path / "archive" / audit.name).exists()


# --- verification catches discrepancies --------------------------------------------------
def test_verify_reports_a_changed_amount(small: SeedPlan) -> None:
    bank, manifest = seed_fake(small)
    acc = manifest.accounts["priya/checking"]
    record = bank._store.deposits[acc][0]
    bank._store.deposits[acc][0] = record.model_copy(update={"amount": record.amount + 1})
    report = verify(small, bank, manifest)
    assert any("bank has" in p for p in report.problems)
    assert any("balance" in p for p in report.problems)  # and the balance no longer matches


def test_verify_reports_a_changed_status(small: SeedPlan) -> None:
    bank, manifest = seed_fake(small)
    acc = manifest.accounts["priya/checking"]
    record = bank._store.deposits[acc][0]
    bank._store.deposits[acc][0] = record.model_copy(update={"status": TxStatus.PENDING})
    assert any("bank has" in p for p in verify(small, bank, manifest).problems)


def test_verify_reports_a_missing_record(small: SeedPlan) -> None:
    bank, manifest = seed_fake(small)
    acc = manifest.accounts["priya/checking"]
    bank._store.deposits[acc].pop(0)
    assert any("missing from" in p for p in verify(small, bank, manifest).problems)


def test_verify_reports_an_unexpected_record(small: SeedPlan) -> None:
    bank, manifest = seed_fake(small)
    bank.create_withdrawal(manifest.accounts["priya/checking"], 5, ANCHOR, "surprise")
    assert any("unexpected withdrawal" in p for p in verify(small, bank, manifest).problems)


def test_verify_reports_a_purchase_with_the_wrong_merchant(small: SeedPlan) -> None:
    """D47: Nessie would happily store a purchase whose merchant id is wrong or unknown."""
    bank, manifest = seed_fake(small)
    card = manifest.accounts["priya/card"]
    record = bank._store.purchases[card][0]
    bank._store.purchases[card][0] = record.model_copy(update={"merchant_id": "not-a-merchant"})
    assert any("merchant differs" in p for p in verify(small, bank, manifest).problems)


def test_verify_reports_a_changed_bill(small: SeedPlan) -> None:
    bank, manifest = seed_fake(small)
    acc = manifest.accounts["priya/checking"]
    bill = bank._store.bills[acc][0]
    bank._store.bills[acc][0] = bill.model_copy(update={"recurring_date": 28})
    assert any("fields differ" in p for p in verify(small, bank, manifest).problems)


def test_verify_reports_a_deleted_account(small: SeedPlan) -> None:
    bank, manifest = seed_fake(small)
    bank.delete_account(manifest.accounts["priya/savings"])
    assert "account priya/savings: missing at the bank" in verify(small, bank, manifest).problems


def test_verify_reports_a_wrong_opening_balance(small: SeedPlan) -> None:
    bank, manifest = seed_fake(small)
    acc = manifest.accounts["priya/savings"]
    bank._store.accounts[acc] = bank._store.accounts[acc].model_copy(update={"balance": 1})
    problems = verify(small, bank, manifest).problems
    assert any("opening balance" in p for p in problems)


def test_verify_refuses_a_partial_seed_or_another_plan(small: SeedPlan) -> None:
    bank, manifest = seed_fake(small)
    manifest.status = SeedStatus.PARTIAL
    assert "not complete" in verify(small, bank, manifest).problems[0]
    manifest.status = SeedStatus.COMPLETE
    other = generate(43, ANCHOR).subset("priya", 15)
    assert "different plan" in verify(other, bank, manifest).problems[0]


def test_verify_retries_a_temporary_read_once(small: SeedPlan, monkeypatch) -> None:
    bank, manifest = seed_fake(small)
    real = bank.list_bills
    calls = {"n": 0}

    def flaky(account_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise NessieError("HTTP 503", status_code=503)
        return real(account_id)

    monkeypatch.setattr(bank, "list_bills", flaky)
    sleeps: list[float] = []
    assert verify(small, bank, manifest, sleep=sleeps.append).ok
    assert sleeps == [0.5]


def test_verify_does_not_retry_a_permanent_error(small: SeedPlan, monkeypatch) -> None:
    bank, manifest = seed_fake(small)

    def unauthorized(account_id):
        raise NessieError("HTTP 401", status_code=401)

    monkeypatch.setattr(bank, "list_bills", unauthorized)
    sleeps: list[float] = []
    report = verify(small, bank, manifest, sleep=sleeps.append)
    assert sleeps == []
    # R2-16: recorded as a problem for every account, not a crash; the other
    # accounts' checks still ran (D51: collect all problems)
    failed = [p for p in report.problems if "a read failed" in p]
    assert len(failed) == len(small.accounts)
    assert report.checked["accounts"] == len(small.accounts)


def test_verify_records_a_bad_record_as_a_problem(small: SeedPlan, monkeypatch) -> None:
    """R2-16: malformed bank data (NessieDataError) is reported, not a traceback."""
    from nessie_tools import NessieDataError

    bank, manifest = seed_fake(small)

    def garbled(customer_id):
        raise NessieDataError("expected a list")

    monkeypatch.setattr(bank, "list_accounts", garbled)
    report = verify(small, bank, manifest)
    assert not report.ok
    assert any("could not be read" in p and "expected a list" in p for p in report.problems)


def test_verify_reports_an_account_the_manifest_does_not_list(small: SeedPlan) -> None:
    """R2-06: an extra account under a seed customer (e.g. created by a write that timed
    out, so it never reached the manifest) is a problem."""
    bank, manifest = seed_fake(small)
    stray = bank.create_account(manifest.customers["priya"], AccountType.SAVINGS, "Savings", 1200)
    report = verify(small, bank, manifest)
    assert [p for p in report.problems if "not part of this seed" in p] == [
        f"customer priya: account {stray.id} is not part of this seed (R2-06)"
    ]


def test_a_timed_out_account_create_never_leaves_a_hidden_account(small: SeedPlan) -> None:
    """R2-06 end to end: the create times out after the bank wrote the account. Reset
    can't see it, but the reseed won't reuse that customer, so the agent never sees a
    stray account, and verify stays honest."""
    bank, registry = FakeBackend(seed=1), Registry()
    saved: list[Manifest] = []
    real, calls = bank.create_account, {"n": 0}

    def times_out_on_the_second(*args, **kwargs):
        calls["n"] += 1
        created = real(*args, **kwargs)
        if calls["n"] == 2:
            raise BackendFailure("timeout after writing", ambiguous=True)
        return created

    bank.create_account = times_out_on_the_second  # type: ignore[method-assign]
    with pytest.raises(BackendFailure):
        apply_plan(
            small,
            bank,
            server="fake",
            registry=registry,
            save=lambda m: saved.append(Manifest.from_json(m.to_json())),
        )
    aborted = saved[-1]
    assert len(aborted.accounts) == 1  # the timed-out account is not listed
    assert reset(bank, aborted, server="fake") == []
    old_customer = aborted.customers["priya"]
    assert len(bank.list_accounts(old_customer)) == 1  # the stray one survived reset
    bank.create_account = real  # type: ignore[method-assign]
    (stray,) = bank.list_accounts(old_customer)
    warnings: list[str] = []
    again = apply_plan(small, bank, server="fake", registry=registry, on_warning=warnings.append)
    assert again.customers["priya"] != old_customer  # not reused
    # V2-06: the abandoned customer and what it still holds are reported and remembered
    assert len(warnings) == 1 and old_customer in warnings[0] and stray.id in warnings[0]
    assert stray.id in registry.abandoned("fake")[old_customer]
    assert len(bank.list_accounts(again.customers["priya"])) == len(small.accounts)
    assert verify(small, bank, again).ok


# --- reuse of merchants and customers (D51) ---------------------------------------------
def test_second_seed_reuses_merchants_and_customers(small: SeedPlan) -> None:
    bank, registry = FakeBackend(seed=1), Registry()
    first = apply_plan(small, bank, server="fake", registry=registry)
    assert reset(bank, first, server="fake") == []
    second = apply_plan(small, bank, server="fake", registry=registry)
    assert second.merchants == first.merchants
    assert second.customers == first.customers
    assert set(second.accounts.values()).isdisjoint(first.accounts.values())
    assert second.timing["writes"] == 23  # 33 minus 9 merchants and 1 customer
    assert verify(small, bank, second).ok


def test_a_reused_merchant_must_still_match(small: SeedPlan) -> None:
    bank, registry = FakeBackend(seed=1), Registry()
    key = small.merchants[0].key
    impostor = bank.create_merchant(small.merchants[0].name, "not-the-category")
    registry.set("fake", "merchants", key, impostor.id)
    registry.set("fake", "merchants", small.merchants[1].key, "id-the-bank-never-had")
    manifest = apply_plan(small, bank, server="fake", registry=registry)
    assert manifest.merchants[key] != impostor.id  # a new, correct merchant
    assert registry.get("fake", "merchants", key) == manifest.merchants[key]
    assert manifest.merchants[small.merchants[1].key] != "id-the-bank-never-had"
    assert verify(small, bank, manifest).ok


def test_registry_is_per_server(small: SeedPlan) -> None:
    registry = Registry()
    apply_plan(small, FakeBackend(seed=1), server="fake", registry=registry)
    assert registry.get("https://other", "merchants", small.merchants[0].key) is None


# --- files -------------------------------------------------------------------------------
def test_manifest_round_trips_and_writes_atomically(small: SeedPlan, tmp_path: Path) -> None:
    _, manifest = seed_fake(small)
    store = SeedStore(tmp_path)
    store.save_manifest(manifest)
    loaded = store.load_manifest()
    assert loaded == manifest
    assert [p.name for p in tmp_path.iterdir()] == ["manifest.json"]  # no temp files left


def test_manifest_writes_are_flushed_to_disk(small: SeedPlan, tmp_path: Path, monkeypatch) -> None:
    """R2-19: the new file's data is fsynced BEFORE the rename (and the directory after),
    so a power loss leaves the old or the new manifest, never an empty one."""
    import os
    import stat

    steps: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def recording_fsync(fd: int) -> None:
        steps.append("fsync file" if stat.S_ISREG(os.fstat(fd).st_mode) else "fsync dir")
        real_fsync(fd)

    def recording_replace(src, dst) -> None:
        steps.append("rename")
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(os, "replace", recording_replace)
    _, manifest = seed_fake(small)
    SeedStore(tmp_path).save_manifest(manifest)
    assert steps[:2] == ["fsync file", "rename"]


def test_manifest_with_an_unknown_schema_is_refused(small: SeedPlan) -> None:
    _, manifest = seed_fake(small)
    data = json.loads(manifest.to_json())
    data["schema"] = 99
    with pytest.raises(ValueError, match="schema"):
        Manifest.from_json(json.dumps(data))


def test_subset_is_self_consistent(plan: SeedPlan) -> None:
    small = plan.subset("marcus", 20)
    kept = {t.key for t in small.transactions}
    assert len(small.transactions) == 20 and len(small.customers) == 1
    assert all(b.paid_by_tx_key is None or b.paid_by_tx_key in kept for b in small.bills)
    used = {t.merchant_key for t in small.transactions if t.merchant_key}
    assert {m.key for m in small.merchants} == used
    bank, manifest = seed_fake(small)
    assert verify(small, bank, manifest).ok


# --- command line, with the fake bank plugged in ---------------------------------------
def test_cli_apply_verify_reset_cycle(tmp_path: Path, capsys) -> None:
    bank = FakeBackend(seed=1)

    def factory():
        return bank, "fake", lambda: None

    args = ["--dir", str(tmp_path)]
    assert seed_cli(["apply", "--anchor", ANCHOR.isoformat(), *args], factory) == 0
    out = capsys.readouterr().out
    assert "verified:" in out and "454 transactions" in out
    store = SeedStore(tmp_path)
    manifest = store.load_manifest()
    assert manifest is not None and manifest.status is SeedStatus.COMPLETE
    assert manifest.audit_db and Path(manifest.audit_db).exists()
    assert store.registry_path.exists()

    assert seed_cli(["apply", "--anchor", ANCHOR.isoformat(), *args], factory) == 2  # in place
    assert "already in place" in capsys.readouterr().err

    assert seed_cli(["verify", *args], factory) == 0
    assert seed_cli(["reset", *args], factory) == 0
    reloaded = store.load_manifest()
    assert reloaded is not None and reloaded.status is SeedStatus.RESET
    assert not Path(manifest.audit_db).exists()  # archived
    assert seed_cli(["apply", "--anchor", ANCHOR.isoformat(), *args], factory) == 0


def test_cli_reports_verification_failures(tmp_path: Path, capsys) -> None:
    bank = FakeBackend(seed=1)

    def factory():
        return bank, "fake", lambda: None

    assert seed_cli(["apply", "--anchor", ANCHOR.isoformat(), "--dir", str(tmp_path)], factory) == 0
    manifest = SeedStore(tmp_path).load_manifest()
    assert manifest is not None
    bank.create_deposit(manifest.accounts["elena/savings"], 1, ANCHOR, "stray")
    capsys.readouterr()
    assert seed_cli(["verify", "--dir", str(tmp_path)], factory) == 1
    assert "VERIFICATION FAILED" in capsys.readouterr().out


def test_cli_without_a_seed_says_what_to_do(tmp_path: Path, capsys) -> None:
    def factory():
        return FakeBackend(seed=1), "fake", lambda: None

    assert seed_cli(["verify", "--dir", str(tmp_path)], factory) == 2
    assert "make seed-live" in capsys.readouterr().err


def test_cli_reports_an_aborted_apply(tmp_path: Path, capsys) -> None:
    bank = FakeBackend(seed=1)
    bank.fail_next("create_deposit")

    def factory():
        return bank, "fake", lambda: None

    rc = seed_cli(["apply", "--anchor", ANCHOR.isoformat(), "--dir", str(tmp_path)], factory)
    assert rc == 1
    assert "make reset-live" in capsys.readouterr().err
    manifest = SeedStore(tmp_path).load_manifest()
    assert manifest is not None and manifest.status is SeedStatus.PARTIAL
    assert seed_cli(["reset", "--dir", str(tmp_path)], factory) == 0


def _address():
    from ledgervoice.seed.apply import SEED_ADDRESS

    return SEED_ADDRESS


def test_cli_verify_reports_a_missing_audit_database(tmp_path: Path, capsys) -> None:
    """R2-18: a missing audit DB must not be silently re-created empty (which would
    pass reconcile)."""
    bank = FakeBackend(seed=1)

    def factory():
        return bank, "fake", lambda: None

    args = ["--dir", str(tmp_path)]
    assert seed_cli(["apply", "--anchor", ANCHOR.isoformat(), *args], factory) == 0
    manifest = SeedStore(tmp_path).load_manifest()
    assert manifest is not None and manifest.audit_db
    Path(manifest.audit_db).unlink()
    capsys.readouterr()
    assert seed_cli(["verify", *args], factory) == 1
    assert "audit database" in capsys.readouterr().out
    assert not Path(manifest.audit_db).exists()  # not re-created
