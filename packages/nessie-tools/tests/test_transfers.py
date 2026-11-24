"""The transfer saga: the two-step API, every outcome branch, pre-checks, concurrency."""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from conftest import TODAY
from nessie_tools import (
    AuditLog,
    NessieDataError,
    NessieError,
    TransferRejected,
    TransferService,
    TransferStatus,
    ViolationKind,
    compute_balance,
    reconcile,
)

S = TransferStatus
V = ViolationKind


def balances(bank):
    return (
        compute_balance(bank.fake, bank.checking.id).current,
        compute_balance(bank.fake, bank.savings.id).current,
    )


def propose(bank, amount=50):
    return bank.service.create_pending(bank.customer_id, bank.checking.id, bank.savings.id, amount)


def transfer(bank, amount=50):
    """Propose + execute: the only way money can move."""
    pending = propose(bank, amount)
    return bank.service.execute_pending(pending.transfer_id, customer_id=bank.customer_id)


def history(bank, tid):
    return [e.status for e in bank.audit.events(tid)]


def last_detail(bank, tid):
    return bank.audit.events(tid)[-1].detail


def report(bank):
    return reconcile(bank.fake, bank.audit, [bank.checking.id, bank.savings.id])


def assert_reconciles(bank):
    r = report(bank)
    assert r.ok, r.violations


# --- the two-step API (R1-01) ------------------------------------------------------
def test_propose_moves_no_money(bank):
    record = propose(bank)
    assert record.status is S.PENDING
    assert balances(bank) == (1000, 500)
    assert bank.fake.list_withdrawals(bank.checking.id) == []


def test_completed_transfer_moves_money(bank):
    result = transfer(bank)
    assert result.status is S.COMPLETED and result.succeeded
    assert balances(bank) == (950, 550)
    assert history(bank, result.transfer_id) == [S.PENDING, S.EXECUTING, S.LEG1_DONE, S.COMPLETED]
    assert f"account ending in {bank.checking.last4}" in result.message  # spoken, not "••"
    assert_reconciles(bank)


def test_legs_carry_the_transfer_id(bank):
    tid = transfer(bank).transfer_id
    [w] = bank.fake.list_withdrawals(bank.checking.id)
    [d] = bank.fake.list_deposits(bank.savings.id)
    assert w.description.startswith(tid) and d.description.startswith(tid)


@pytest.mark.parametrize("bad_id", ["LV-TRF-0badc0de", "LV-TRF-hallucinated", "confirm 42", ""])
def test_unknown_or_invented_id_moves_nothing(bank, bad_id):
    with pytest.raises(TransferRejected):
        bank.service.execute_pending(bad_id, customer_id=bank.customer_id)
    assert bank.audit.records() == []
    assert balances(bank) == (1000, 500)


def test_finished_transfer_cannot_run_again(bank):
    tid = transfer(bank).transfer_id
    with pytest.raises(TransferRejected):
        bank.service.execute_pending(tid, customer_id=bank.customer_id)
    assert len(bank.fake.list_withdrawals(bank.checking.id)) == 1


def test_cancelled_and_expired_transfers_cannot_run(bank):
    a, b = propose(bank), propose(bank, 20)
    bank.service.cancel_pending(a.transfer_id, customer_id=bank.customer_id, reason="user said no")
    bank.service.cancel_pending(b.transfer_id, customer_id=bank.customer_id, expired=True)
    assert bank.audit.get(a.transfer_id).status is S.CANCELLED
    assert bank.audit.get(b.transfer_id).status is S.EXPIRED
    for tid in (a.transfer_id, b.transfer_id):
        with pytest.raises(TransferRejected):
            bank.service.execute_pending(tid, customer_id=bank.customer_id)
    assert balances(bank) == (1000, 500)
    assert_reconciles(bank)


def test_cancel_requires_pending(bank):
    tid = transfer(bank).transfer_id
    with pytest.raises(TransferRejected):
        bank.service.cancel_pending(tid, customer_id=bank.customer_id)


def test_funds_rechecked_at_execution(bank):
    """Proposed while affordable; by the time the user says yes, it no longer is."""
    first, second = propose(bank, 600), propose(bank, 600)
    assert bank.service.execute_pending(first.transfer_id, customer_id=bank.customer_id).succeeded
    with pytest.raises(TransferRejected, match="insufficient"):
        bank.service.execute_pending(second.transfer_id, customer_id=bank.customer_id)
    assert bank.audit.get(second.transfer_id).status is S.CANCELLED
    assert balances(bank) == (400, 1100)


# --- leg 1 outcomes ----------------------------------------------------------------
def test_leg1_rejected_means_nothing_moved(bank):
    bank.fake.fail_next("create_withdrawal")
    result = transfer(bank)
    assert result.status is S.FAILED
    assert balances(bank) == (1000, 500)
    assert "injected failure" in last_detail(bank, result.transfer_id)  # reason recorded
    assert_reconciles(bank)


def test_leg1_ambiguous_but_written_is_not_duplicated(bank):
    bank.fake.fail_next("create_withdrawal", ambiguous=True)
    result = transfer(bank)
    assert result.status is S.COMPLETED
    assert len(bank.fake.list_withdrawals(bank.checking.id)) == 1
    assert_reconciles(bank)


def test_leg1_late_write_found_on_a_recheck(bank):
    bank.fake.fail_next("create_withdrawal", appears_after_lists=2)
    result = transfer(bank)
    assert result.status is S.COMPLETED
    assert len(bank.fake.list_withdrawals(bank.checking.id)) == 1
    assert bank.sleeps  # it waited between checks
    assert_reconciles(bank)


def test_leg1_late_write_never_seen_is_needs_attention_not_failed(bank):
    """R1-02: 'not found yet' is not 'not written'. Never tell the user no money moved."""
    bank.fake.fail_next("create_withdrawal", appears_after_lists=100)
    result = transfer(bank)
    assert result.status is S.NEEDS_ATTENTION
    assert "can't confirm" in result.message and result.transfer_id in result.message
    bank.fake.flush_late_writes()  # the write lands after all
    assert balances(bank) == (950, 500)  # which is exactly why FAILED would have been a lie
    assert bank.sleeps == [0.5, 1.0, 2.0]


def test_leg1_unverifiable_needs_attention(bank, monkeypatch):
    def timeout(*args, **kwargs):
        raise NessieError("ReadTimeout")

    attempted: list[bool] = []
    real_list = bank.fake.list_withdrawals

    def list_down_after_write(account_id):
        if attempted:
            raise NessieError("HTTP 503", status_code=503)
        return real_list(account_id)

    def create(*args, **kwargs):
        attempted.append(True)
        timeout()

    monkeypatch.setattr(bank.fake, "create_withdrawal", create)
    monkeypatch.setattr(bank.fake, "list_withdrawals", list_down_after_write)
    result = transfer(bank)
    assert result.status is S.NEEDS_ATTENTION
    assert "last check failed" in last_detail(bank, result.transfer_id)


def test_2xx_with_unreadable_body_is_treated_as_written(bank, monkeypatch):
    """R1-03: a 2xx means the bank accepted it, even if we couldn't parse the reply."""
    real_create = bank.fake.create_deposit

    def accepted_then_garbled(*args, **kwargs):
        real_create(*args, **kwargs)
        raise NessieError("returned non-JSON body", status_code=201)

    monkeypatch.setattr(bank.fake, "create_deposit", accepted_then_garbled)
    result = transfer(bank)
    assert result.status is S.COMPLETED  # found by the re-check, not compensated
    assert balances(bank) == (950, 550)
    assert_reconciles(bank)


# --- leg 2 outcomes ----------------------------------------------------------------
def test_leg2_rejected_is_compensated(bank):
    bank.fake.fail_next("create_deposit")
    result = transfer(bank)
    assert result.status is S.COMPENSATED
    assert balances(bank) == (1000, 500)
    assert history(bank, result.transfer_id)[-1] is S.COMPENSATED
    assert bank.audit.get(result.transfer_id).reversal_id is not None
    assert "leg 2 rejected" in last_detail(bank, result.transfer_id)
    assert_reconciles(bank)


def test_leg2_ambiguous_but_written_completes(bank):
    bank.fake.fail_next("create_deposit", ambiguous=True)
    result = transfer(bank)
    assert result.status is S.COMPLETED
    assert len(bank.fake.list_deposits(bank.savings.id)) == 1
    assert_reconciles(bank)


def test_leg2_late_write_found_on_a_recheck(bank):
    bank.fake.fail_next("create_deposit", appears_after_lists=1)
    result = transfer(bank)
    assert result.status is S.COMPLETED
    assert bank.fake.list_deposits(bank.checking.id) == []  # no reversal
    assert_reconciles(bank)


def test_leg2_unknown_is_not_compensated(bank):
    """R1-02: compensating while leg 2 may still land could create money."""
    bank.fake.fail_next("create_deposit", appears_after_lists=100)
    result = transfer(bank)
    assert result.status is S.NEEDS_ATTENTION
    assert bank.fake.list_deposits(bank.checking.id) == []  # no reversal written
    bank.fake.flush_late_writes()
    assert balances(bank) == (950, 550)  # totals still add up to 1500


# --- compensation outcomes ----------------------------------------------------------
def test_reversal_rejected_needs_attention(bank):
    bank.fake.fail_next("create_deposit")  # leg 2
    bank.fake.fail_next("create_deposit")  # reversal
    result = transfer(bank)
    assert result.status is S.NEEDS_ATTENTION
    assert balances(bank) == (950, 500)  # money is out of checking: a human must fix it
    assert report(bank).count(V.NEEDS_ATTENTION) == 1


def test_reversal_ambiguous_but_written_is_compensated(bank):
    bank.fake.fail_next("create_deposit")  # leg 2 rejected
    bank.fake.fail_next("create_deposit", ambiguous=True)  # reversal written, then timeout
    result = transfer(bank)
    assert result.status is S.COMPENSATED
    assert balances(bank) == (1000, 500)
    assert_reconciles(bank)


def test_reversal_unknown_needs_attention(bank):
    bank.fake.fail_next("create_deposit")
    bank.fake.fail_next("create_deposit", appears_after_lists=100)
    result = transfer(bank)
    assert result.status is S.NEEDS_ATTENTION
    assert "reversal unknown" in last_detail(bank, result.transfer_id)


# --- pre-checks: rejected before anything is recorded --------------------------------
@pytest.mark.parametrize("amount", [0, -5, 12.5, True])
def test_bad_amounts_are_rejected(bank, amount):
    with pytest.raises(TransferRejected):
        propose(bank, amount)
    assert bank.audit.records() == []


def test_insufficient_funds(bank):
    with pytest.raises(TransferRejected, match="insufficient"):
        propose(bank, 1001)
    assert bank.audit.records() == []


def test_same_account(bank):
    with pytest.raises(TransferRejected):
        bank.service.create_pending(bank.customer_id, bank.checking.id, bank.checking.id, 10)


def test_someone_elses_account(bank):
    with pytest.raises(TransferRejected, match="belong"):
        bank.service.create_pending(
            bank.customer_id, bank.checking.id, bank.other_customer_account.id, 10
        )


def test_unknown_account_never_receives_a_leg(bank):
    """D36: the bank would accept a deposit for a made-up account; the saga must not try."""
    with pytest.raises(TransferRejected):
        bank.service.create_pending(bank.customer_id, bank.checking.id, "no-such-account", 10)
    assert bank.fake.list_deposits("no-such-account") == []
    assert bank.fake.list_withdrawals(bank.checking.id) == []


def test_destination_deleted_after_proposal_is_cancelled(bank):
    pending = propose(bank)
    bank.fake.delete_account(bank.savings.id)
    with pytest.raises(TransferRejected):
        bank.service.execute_pending(pending.transfer_id, customer_id=bank.customer_id)
    assert bank.audit.get(pending.transfer_id).status is S.CANCELLED
    assert bank.fake.list_withdrawals(bank.checking.id) == []
    assert bank.fake.list_deposits(bank.savings.id) == []


def test_balance_read_failure_is_a_rejection(bank, monkeypatch):
    def down(*args, **kwargs):
        raise NessieError("HTTP 503", status_code=503)

    monkeypatch.setattr(bank.fake, "list_deposits", down)
    with pytest.raises(TransferRejected, match="balance"):
        propose(bank)
    assert bank.audit.records() == []


def test_balance_check_uses_computed_balance(bank):
    """After moving $950 out, the stored balance still says 1000; the check must not."""
    transfer(bank, 950)
    with pytest.raises(TransferRejected, match="insufficient"):
        propose(bank, 100)


# --- concurrency (R1-08) ---------------------------------------------------------------
def test_concurrent_executions_cannot_overspend(bank):
    first, second = propose(bank, 700), propose(bank, 700)
    results, errors = [], []

    def run(tid):
        try:
            results.append(bank.service.execute_pending(tid, customer_id=bank.customer_id))
        except TransferRejected as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(t.transfer_id,)) for t in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 1 and results[0].succeeded
    assert len(errors) == 1
    assert balances(bank) == (300, 1200)
    assert_reconciles(bank)


def test_two_service_instances_cannot_overspend(bank, monkeypatch):
    """V1-01: the account lock is shared by every TransferService in the process."""
    other = TransferService(bank.fake, bank.audit, today=lambda: TODAY, sleep=lambda _: None)
    first, second = propose(bank, 700), propose(bank, 700)
    outcomes = []
    # Make the race deterministic: without a shared lock both threads meet here and
    # both read the 1000 balance before either writes. With the lock, the first
    # thread waits out the timeout alone and the second never gets this far early.
    meet = threading.Barrier(2, timeout=1.0)
    real_list = bank.fake.list_withdrawals

    def list_after_meeting(account_id):
        try:
            meet.wait()
        except threading.BrokenBarrierError:
            pass
        return real_list(account_id)

    monkeypatch.setattr(bank.fake, "list_withdrawals", list_after_meeting)

    def run(service, tid):
        try:
            outcomes.append(service.execute_pending(tid, customer_id=bank.customer_id))
        except TransferRejected as exc:
            outcomes.append(exc)

    threads = [
        threading.Thread(target=run, args=(bank.service, first.transfer_id)),
        threading.Thread(target=run, args=(other, second.transfer_id)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for o in outcomes if isinstance(o, TransferRejected)) == 1
    assert balances(bank) == (300, 1200)
    assert_reconciles(bank)


def test_other_customers_cannot_execute_or_cancel(bank):
    """V1-06: a session can only act on its own customer's pending transfers."""
    pending = propose(bank)
    stranger = bank.other_customer_account.customer_id
    with pytest.raises(TransferRejected, match="no transfer"):
        bank.service.execute_pending(pending.transfer_id, customer_id=stranger)
    with pytest.raises(TransferRejected, match="no transfer"):
        bank.service.cancel_pending(pending.transfer_id, customer_id=stranger)
    assert bank.audit.get(pending.transfer_id).status is S.PENDING  # untouched
    assert balances(bank) == (1000, 500)


def test_temporary_check_failure_keeps_the_transfer_pending(bank, monkeypatch):
    """V1-04: a blip right after the user's yes must not throw the transfer away."""
    pending = propose(bank)
    real = bank.fake.list_deposits
    calls = []

    def flaky(account_id):
        calls.append(1)
        if len(calls) == 1:
            raise NessieError("HTTP 503", status_code=503)
        return real(account_id)

    monkeypatch.setattr(bank.fake, "list_deposits", flaky)
    with pytest.raises(TransferRejected) as info:
        bank.service.execute_pending(pending.transfer_id, customer_id=bank.customer_id)
    assert info.value.retryable
    assert bank.audit.get(pending.transfer_id).status is S.PENDING
    result = bank.service.execute_pending(pending.transfer_id, customer_id=bank.customer_id)
    assert result.succeeded


def test_permanent_check_failure_cancels(bank):
    pending = propose(bank)
    bank.fake.delete_account(bank.savings.id)
    with pytest.raises(TransferRejected) as info:
        bank.service.execute_pending(pending.transfer_id, customer_id=bank.customer_id)
    assert not info.value.retryable
    assert bank.audit.get(pending.transfer_id).status is S.CANCELLED


def test_destination_deleted_after_leg1_is_compensated(bank, monkeypatch):
    """V1-03: the bank would accept the deposit for a deleted account; we must not send it."""
    real = bank.fake.create_withdrawal

    def withdraw_then_delete_destination(*args, **kwargs):
        leg = real(*args, **kwargs)
        bank.fake.delete_account(bank.savings.id)  # e.g. a reset running concurrently
        return leg

    monkeypatch.setattr(bank.fake, "create_withdrawal", withdraw_then_delete_destination)
    result = transfer(bank)
    assert result.status is S.COMPENSATED
    assert "returned" in result.message
    assert bank.fake.list_deposits(bank.savings.id) == []  # nothing sent into the void
    assert compute_balance(bank.fake, bank.checking.id).current == 1000
    assert "no longer exists" in last_detail(bank, result.transfer_id)


def fail_get_account(bank, monkeypatch, account_id, *, on_call, error):
    """Make the `on_call`-th get_account(account_id) during execution raise `error`.

    Call 1 is the execution pre-check; call 2 is the mid-transfer check.
    """
    real = bank.fake.get_account
    calls = {"n": 0}

    def flaky(requested_id):
        if requested_id == account_id:
            calls["n"] += 1
            if calls["n"] == on_call:
                raise error
        return real(requested_id)

    monkeypatch.setattr(bank.fake, "get_account", flaky)


def test_unverifiable_destination_before_leg2_is_compensated(bank, monkeypatch):
    """N2 (D40): "couldn't check" must not count as "exists".

    Nothing has been written to the destination yet, so returning the money is safe."""
    pending = propose(bank)
    fail_get_account(
        bank,
        monkeypatch,
        bank.savings.id,
        on_call=2,
        error=NessieError("HTTP 503", status_code=503),
    )
    result = bank.service.execute_pending(pending.transfer_id, customer_id=bank.customer_id)
    assert result.status is S.COMPENSATED
    assert bank.fake.list_deposits(bank.savings.id) == []  # leg 2 never sent
    assert balances(bank) == (1000, 500)
    assert "could not verify the destination" in last_detail(bank, result.transfer_id)
    assert_reconciles(bank)


def leg2_rejected_and_then(bank, monkeypatch, side_effect):
    """Leg 2 is cleanly rejected (4xx-like); `side_effect` runs at that moment."""
    real = bank.fake.create_deposit
    calls = {"n": 0}

    def deposit(account_id, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # leg 2
            side_effect()
            raise NessieError("HTTP 400 rejected", status_code=400)
        return real(account_id, *args, **kwargs)

    monkeypatch.setattr(bank.fake, "create_deposit", deposit)


def test_reversal_is_not_sent_to_a_deleted_source(bank, monkeypatch):
    """N1 (D40): never tell the user "returned" when it would land in a deleted account."""
    leg2_rejected_and_then(bank, monkeypatch, lambda: bank.fake.delete_account(bank.checking.id))
    result = transfer(bank)
    assert result.status is S.NEEDS_ATTENTION
    assert "returned" not in result.message
    assert bank.fake.list_deposits(bank.checking.id) == []  # no reversal written
    detail = last_detail(bank, result.transfer_id)
    assert "source account no longer exists" in detail
    assert "reversal not written" in detail


def test_reversal_waits_when_the_source_cannot_be_verified(bank, monkeypatch):
    pending = propose(bank)
    leg2_rejected_and_then(bank, monkeypatch, lambda: None)
    fail_get_account(
        bank,
        monkeypatch,
        bank.checking.id,
        on_call=2,
        error=NessieError("timeout", status_code=None),
    )
    result = bank.service.execute_pending(pending.transfer_id, customer_id=bank.customer_id)
    assert result.status is S.NEEDS_ATTENTION
    assert bank.fake.list_deposits(bank.checking.id) == []
    assert "could not verify the source account" in last_detail(bank, result.transfer_id)


@pytest.mark.parametrize(
    ("status_code", "retryable", "final"),
    [
        (None, True, S.PENDING),  # network failure
        (503, True, S.PENDING),
        (429, True, S.PENDING),  # rate limited: try later
        (401, False, S.CANCELLED),  # bad key: will never work
        (403, False, S.CANCELLED),
        (400, False, S.CANCELLED),
    ],
)
def test_only_temporary_failures_are_retryable(bank, monkeypatch, status_code, retryable, final):
    """N3: a retry loop on a 401 would never end."""
    pending = propose(bank)
    fail_get_account(
        bank,
        monkeypatch,
        bank.checking.id,
        on_call=1,
        error=NessieError(f"HTTP {status_code}", status_code=status_code),
    )
    with pytest.raises(TransferRejected) as info:
        bank.service.execute_pending(pending.transfer_id, customer_id=bank.customer_id)
    assert info.value.retryable is retryable
    assert bank.audit.get(pending.transfer_id).status is final


def test_balance_read_failure_uses_the_same_rule(bank, monkeypatch):
    pending = propose(bank)

    def unauthorized(account_id):
        raise NessieError("HTTP 401", status_code=401)

    monkeypatch.setattr(bank.fake, "list_deposits", unauthorized)
    with pytest.raises(TransferRejected) as info:
        bank.service.execute_pending(pending.transfer_id, customer_id=bank.customer_id)
    assert not info.value.retryable
    assert bank.audit.get(pending.transfer_id).status is S.CANCELLED


def test_malformed_bank_data_cancels_instead_of_retrying_forever(bank, monkeypatch):
    """V2 finding 5 (D41): bad data has no HTTP status but is not temporary."""
    pending = propose(bank)

    def malformed(account_id):
        raise NessieDataError("GET /accounts/x/purchases: unexpected Purchase data (bad amount)")

    monkeypatch.setattr(bank.fake, "list_purchases", malformed)
    with pytest.raises(TransferRejected) as info:
        bank.service.execute_pending(pending.transfer_id, customer_id=bank.customer_id)
    assert not info.value.retryable
    assert bank.audit.get(pending.transfer_id).status is S.CANCELLED
    assert "unexpected Purchase data" in last_detail(bank, pending.transfer_id)


def test_unreadable_create_response_is_still_treated_as_possibly_written(bank, monkeypatch):
    """D41 must not weaken D33: a garbled create response means "go and look", not "failed"."""
    real = bank.fake.create_withdrawal

    def written_but_garbled(*args, **kwargs):
        real(*args, **kwargs)
        raise NessieDataError("POST /accounts/x/withdrawals: response has no created object id")

    monkeypatch.setattr(bank.fake, "create_withdrawal", written_but_garbled)
    result = transfer(bank)
    assert result.status is S.COMPLETED  # the re-check found leg 1; no duplicate written
    assert len(bank.fake.list_withdrawals(bank.checking.id)) == 1
    assert balances(bank) == (950, 550)
    assert_reconciles(bank)


class _Clock:
    """One clock for the audit log and the service, moved by hand."""

    def __init__(self) -> None:
        self.t = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t


def _timed_service(bank, max_age=timedelta(minutes=2)):
    clock = _Clock()
    audit = AuditLog(":memory:", now=clock)
    service = TransferService(
        bank.fake, audit, today=lambda: TODAY, sleep=lambda _: None, now=clock, max_age=max_age
    )
    pending = service.create_pending(bank.customer_id, bank.checking.id, bank.savings.id, 50)
    return clock, audit, service, pending


def test_a_fresh_proposal_can_be_executed(bank):
    clock, audit, service, pending = _timed_service(bank)
    clock.t += timedelta(seconds=119)
    assert service.execute_pending(pending.transfer_id, customer_id=bank.customer_id).succeeded


def test_a_stale_proposal_expires_instead_of_executing(bank):
    """D54: even if the runtime forgot to expire it, nessie-tools refuses a stale yes."""
    clock, audit, service, pending = _timed_service(bank)
    clock.t += timedelta(minutes=2, seconds=1)
    with pytest.raises(TransferRejected, match="expired") as info:
        service.execute_pending(pending.transfer_id, customer_id=bank.customer_id)
    assert not info.value.retryable
    assert audit.get(pending.transfer_id).status is S.EXPIRED
    assert "expired" in audit.events(pending.transfer_id)[-1].detail
    assert bank.fake.list_withdrawals(bank.checking.id) == []  # no money moved


def test_max_age_none_disables_the_check(bank):
    clock, audit, service, pending = _timed_service(bank, max_age=None)
    clock.t += timedelta(hours=5)
    assert service.execute_pending(pending.transfer_id, customer_id=bank.customer_id).succeeded
