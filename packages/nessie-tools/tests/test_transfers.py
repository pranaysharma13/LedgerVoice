"""The transfer saga: happy path, every failure branch, and pre-checks."""

import pytest
from nessie_tools import (
    NessieError,
    TransferRejected,
    TransferStatus,
    compute_balance,
    reconcile,
)

S = TransferStatus


def balances(bank):
    return (
        compute_balance(bank.fake, bank.checking.id).current,
        compute_balance(bank.fake, bank.savings.id).current,
    )


def transfer(bank, amount=50, **kw):
    return bank.service.execute(bank.customer_id, bank.checking.id, bank.savings.id, amount, **kw)


def history(bank, tid):
    return [e.status for e in bank.audit.events(tid)]


def assert_reconciles(bank):
    report = reconcile(bank.fake, bank.audit, [bank.checking.id, bank.savings.id])
    assert report.ok, report.violations


# --- happy path ------------------------------------------------------------------
def test_completed_transfer_moves_money(bank):
    result = transfer(bank)
    assert result.status is S.COMPLETED and result.succeeded
    assert balances(bank) == (950, 550)
    assert history(bank, result.transfer_id) == [S.EXECUTING, S.LEG1_DONE, S.COMPLETED]
    assert f"••{bank.checking.last4}" in result.message
    assert_reconciles(bank)


def test_legs_carry_the_transfer_id(bank):
    tid = transfer(bank).transfer_id
    [w] = bank.fake.list_withdrawals(bank.checking.id)
    [d] = bank.fake.list_deposits(bank.savings.id)
    assert w.description.startswith(tid) and d.description.startswith(tid)


# --- leg 1 failures --------------------------------------------------------------
def test_leg1_rejected_means_nothing_moved(bank):
    bank.fake.fail_next("create_withdrawal")
    result = transfer(bank)
    assert result.status is S.FAILED
    assert balances(bank) == (1000, 500)
    assert_reconciles(bank)


def test_leg1_ambiguous_but_written_is_not_duplicated(bank):
    bank.fake.fail_next("create_withdrawal", ambiguous=True)  # written, then "timeout"
    result = transfer(bank)
    assert result.status is S.COMPLETED
    assert len(bank.fake.list_withdrawals(bank.checking.id)) == 1  # no double withdrawal
    assert balances(bank) == (950, 550)
    assert_reconciles(bank)


def test_leg1_timeout_without_write_is_a_clean_failure(bank, monkeypatch):
    def timeout(*args, **kwargs):
        raise NessieError("ReadTimeout")  # no status code: ambiguous

    monkeypatch.setattr(bank.fake, "create_withdrawal", timeout)
    result = transfer(bank)
    assert result.status is S.FAILED  # checked: nothing was written
    assert balances(bank) == (1000, 500)


def test_leg1_unverifiable_needs_attention(bank, monkeypatch):
    def timeout(*args, **kwargs):
        raise NessieError("ReadTimeout")

    real_list = bank.fake.list_withdrawals
    calls = []

    def listing_down_after_precheck(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:  # the balance pre-check still works
            return real_list(*args, **kwargs)
        raise NessieError("HTTP 503", status_code=503)  # the verification lookup fails

    monkeypatch.setattr(bank.fake, "create_withdrawal", timeout)
    monkeypatch.setattr(bank.fake, "list_withdrawals", listing_down_after_precheck)
    result = transfer(bank)
    assert result.status is S.NEEDS_ATTENTION
    assert result.transfer_id in result.message


# --- leg 2 failures --------------------------------------------------------------
def test_leg2_failure_is_compensated(bank):
    bank.fake.fail_next("create_deposit")
    result = transfer(bank)
    assert result.status is S.COMPENSATED
    assert balances(bank) == (1000, 500)  # the reversal put the money back
    assert history(bank, result.transfer_id) == [S.EXECUTING, S.LEG1_DONE, S.COMPENSATED]
    assert bank.audit.get(result.transfer_id).reversal_id is not None
    assert_reconciles(bank)


def test_leg2_ambiguous_but_written_completes(bank):
    bank.fake.fail_next("create_deposit", ambiguous=True)
    result = transfer(bank)
    assert result.status is S.COMPLETED
    assert len(bank.fake.list_deposits(bank.savings.id)) == 1
    assert_reconciles(bank)


def test_leg2_and_reversal_fail_needs_attention(bank):
    bank.fake.fail_next("create_deposit")  # leg 2
    bank.fake.fail_next("create_deposit")  # reversal
    result = transfer(bank)
    assert result.status is S.NEEDS_ATTENTION
    assert balances(bank) == (950, 500)  # money is out of checking: a human must fix it
    report = reconcile(bank.fake, bank.audit, [bank.checking.id, bank.savings.id])
    assert report.count("needs_attention") == 1


# --- pre-checks: rejected before anything is written -------------------------------
@pytest.mark.parametrize("amount", [0, -5, 12.5, True])
def test_bad_amounts_are_rejected(bank, amount):
    with pytest.raises(TransferRejected):
        transfer(bank, amount=amount)
    assert bank.audit.records() == []


def test_insufficient_funds(bank):
    with pytest.raises(TransferRejected, match="insufficient"):
        transfer(bank, amount=1001)
    assert bank.audit.records() == []


def test_same_account(bank):
    with pytest.raises(TransferRejected):
        bank.service.execute(bank.customer_id, bank.checking.id, bank.checking.id, 10)


def test_someone_elses_account(bank):
    with pytest.raises(TransferRejected, match="belong"):
        bank.service.execute(bank.customer_id, bank.checking.id, bank.other_customer_account.id, 10)
    assert bank.fake.list_deposits(bank.other_customer_account.id) == []


def test_balance_read_failure_is_a_rejection(bank, monkeypatch):
    def down(*args, **kwargs):
        raise NessieError("HTTP 503", status_code=503)

    monkeypatch.setattr(bank.fake, "list_deposits", down)
    with pytest.raises(TransferRejected, match="balance"):
        transfer(bank)
    assert bank.audit.records() == []


def test_unknown_account(bank):
    with pytest.raises(TransferRejected):
        bank.service.execute(bank.customer_id, bank.checking.id, "no-such-account", 10)


def test_balance_check_uses_computed_balance(bank):
    """After moving $950 out, the stored balance still says 1000; the check must not."""
    transfer(bank, amount=950)
    with pytest.raises(TransferRejected, match="insufficient"):
        transfer(bank, amount=100)


# --- pending records (used by the M1d confirmation flow) -----------------------------
def test_executes_a_matching_pending_record(bank):
    bank.audit.create(
        "LV-TRF-pending1", bank.customer_id, bank.checking.id, bank.savings.id, 50, S.PENDING
    )
    result = transfer(bank, transfer_id="LV-TRF-pending1")
    assert result.status is S.COMPLETED
    assert history(bank, "LV-TRF-pending1") == [S.PENDING, S.EXECUTING, S.LEG1_DONE, S.COMPLETED]


def test_pending_record_must_match(bank):
    bank.audit.create(
        "LV-TRF-pending2", bank.customer_id, bank.checking.id, bank.savings.id, 50, S.PENDING
    )
    with pytest.raises(TransferRejected, match="does not match"):
        transfer(bank, amount=500, transfer_id="LV-TRF-pending2")


def test_finished_record_cannot_run_again(bank):
    tid = transfer(bank).transfer_id
    with pytest.raises(TransferRejected):
        transfer(bank, transfer_id=tid)
    assert len(bank.fake.list_withdrawals(bank.checking.id)) == 1
