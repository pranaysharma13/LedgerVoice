"""Reconciliation catches every way the audit log and the bank can disagree."""

from datetime import date

from nessie_tools import TransferStatus, TxStatus, ViolationKind, reconcile

S = TransferStatus
V = ViolationKind
DAY = date(2026, 11, 19)


def run(bank, **kw):
    return reconcile(bank.fake, bank.audit, [bank.checking.id, bank.savings.id], **kw)


def transfer(bank, amount):
    pending = bank.service.create_pending(
        bank.customer_id, bank.checking.id, bank.savings.id, amount
    )
    return bank.service.execute_pending(pending.transfer_id)


def record(bank, tid, amount=30, status=S.EXECUTING):
    return bank.audit.create(
        tid, bank.customer_id, bank.checking.id, bank.savings.id, amount, status=status
    )


def complete(bank, tid, withdrawal_id=None, deposit_id=None):
    bank.audit.transition(tid, S.LEG1_DONE, withdrawal_id=withdrawal_id)
    bank.audit.transition(tid, S.COMPLETED, deposit_id=deposit_id)


def test_clean_history_has_no_violations(bank):
    transfer(bank, 50)
    bank.fake.fail_next("create_deposit")
    transfer(bank, 20)  # compensated
    bank.fake.fail_next("create_withdrawal")
    transfer(bank, 10)  # failed
    bank.service.cancel_pending(
        bank.service.create_pending(
            bank.customer_id, bank.checking.id, bank.savings.id, 5
        ).transfer_id
    )

    report = run(bank)
    assert report.ok, report.violations
    assert report.transfers_checked == 4


def test_money_moved_without_audit_record_is_caught(bank):
    """The core safety check: a leg with a transfer id but no audit entry."""
    bank.fake.create_withdrawal(bank.checking.id, 75, DAY, "LV-TRF-deadbeef sneaky (leg 1/2)")
    report = run(bank)
    assert report.count(V.UNAUDITED_LEG) == 1
    assert report.violations[0].transfer_id == "LV-TRF-deadbeef"


def test_unrelated_transactions_are_ignored(bank):
    bank.fake.create_withdrawal(bank.checking.id, 40, DAY, "ATM withdrawal")
    assert run(bank).ok


def test_completed_record_without_legs_is_missing_legs(bank):
    record(bank, "LV-TRF-00000a01")
    complete(bank, "LV-TRF-00000a01")
    assert run(bank).count(V.MISSING_LEG) == 2


def test_wrong_amount_is_a_mismatch(bank):
    tid = "LV-TRF-00000a02"
    record(bank, tid)
    w = bank.fake.create_withdrawal(bank.checking.id, 300, DAY, f"{tid} x (leg 1/2)")
    d = bank.fake.create_deposit(bank.savings.id, 30, DAY, f"{tid} x (leg 2/2)")
    complete(bank, tid, w.id, d.id)
    assert run(bank).count(V.LEG_MISMATCH) == 1


def test_leg_in_the_wrong_account_is_a_mismatch(bank):
    tid = "LV-TRF-00000a03"
    record(bank, tid)
    w = bank.fake.create_withdrawal(bank.checking.id, 30, DAY, f"{tid} x (leg 1/2)")
    d = bank.fake.create_deposit(bank.checking.id, 30, DAY, f"{tid} x (leg 2/2)")  # wrong acct
    complete(bank, tid, w.id, d.id)
    assert run(bank).count(V.LEG_MISMATCH) == 1


def test_leg_that_is_not_the_recorded_one_is_a_mismatch(bank):
    tid = "LV-TRF-00000a04"
    record(bank, tid)
    bank.fake.create_withdrawal(bank.checking.id, 30, DAY, f"{tid} x (leg 1/2)")
    d = bank.fake.create_deposit(bank.savings.id, 30, DAY, f"{tid} x (leg 2/2)")
    complete(bank, tid, "some-other-withdrawal-id", d.id)
    assert run(bank).count(V.LEG_MISMATCH) == 1


def test_cancelled_leg_is_not_completed(bank):
    tid = "LV-TRF-00000a05"
    record(bank, tid)
    w = bank.fake.create_withdrawal(
        bank.checking.id, 30, DAY, f"{tid} x (leg 1/2)", TxStatus.CANCELLED
    )
    d = bank.fake.create_deposit(bank.savings.id, 30, DAY, f"{tid} x (leg 2/2)")
    complete(bank, tid, w.id, d.id)
    assert run(bank).count(V.LEG_NOT_COMPLETED) == 1


def test_failed_record_with_a_leg_is_unexpected(bank):
    tid = "LV-TRF-00000a06"
    record(bank, tid)
    bank.audit.transition(tid, S.FAILED)
    bank.fake.create_withdrawal(bank.checking.id, 30, DAY, f"{tid} x (leg 1/2)")
    assert run(bank).count(V.UNEXPECTED_LEG) == 1


def test_compensated_record_with_a_leg2_is_unexpected(bank):
    bank.fake.fail_next("create_deposit")
    tid = transfer(bank, 20).transfer_id
    bank.fake.create_deposit(bank.savings.id, 20, DAY, f"{tid} late (leg 2/2)")
    assert run(bank).count(V.UNEXPECTED_LEG) == 1


def test_unrecognised_leg_of_an_audited_transfer_is_unexpected(bank):
    tid = transfer(bank, 15).transfer_id
    bank.fake.create_withdrawal(bank.checking.id, 15, DAY, f"{tid} something odd")
    assert run(bank).count(V.UNEXPECTED_LEG) == 1


def test_duplicate_leg_is_caught(bank):
    tid = transfer(bank, 25).transfer_id
    bank.fake.create_deposit(bank.savings.id, 25, DAY, f"{tid} again (leg 2/2)")
    assert run(bank).count(V.DUPLICATE_LEG) == 1


def test_stuck_transfers_are_reported(bank):
    record(bank, "LV-TRF-00000a07")
    record(bank, "LV-TRF-00000a08")
    bank.audit.transition("LV-TRF-00000a08", S.LEG1_DONE)
    assert run(bank).count(V.STUCK_IN_PROGRESS) == 2


def test_needs_attention_is_reported_until_resolved(bank):
    bank.fake.fail_next("create_deposit")
    bank.fake.fail_next("create_deposit")
    tid = transfer(bank, 40).transfer_id
    assert run(bank).count(V.NEEDS_ATTENTION) == 1
    bank.audit.transition(tid, S.RESOLVED, detail="manual deposit of $40 back into checking")
    assert run(bank).ok


def test_pending_transfer_with_no_legs_is_fine(bank):
    record(bank, "LV-TRF-00000a09", status=S.PENDING)
    assert run(bank).ok


def test_deleted_account_is_reported_and_its_legs_still_checked(bank):
    transfer(bank, 5)
    bank.fake.delete_account(bank.savings.id)
    report = reconcile(bank.fake, bank.audit)
    assert report.count(V.ACCOUNT_MISSING) == 1
    assert report.count(V.MISSING_LEG) == 0  # the orphaned leg 2 is still found (D36)
    assert report.legs_found == 2


def test_customer_scope_ignores_other_runs(bank):
    """Old records (other customers, deleted accounts) don't raise false alarms."""
    bank.audit.create("LV-TRF-00000b01", "old-customer", "gone-1", "gone-2", 5, status=S.EXECUTING)
    assert not run(bank).ok  # unscoped: stuck + missing accounts
    assert run(bank, customer_ids=[bank.customer_id]).ok


def test_scoped_run_still_flags_unaudited_legs(bank):
    bank.fake.create_withdrawal(bank.checking.id, 9, DAY, "LV-TRF-0000beef x (leg 1/2)")
    assert run(bank, customer_ids=[bank.customer_id]).count(V.UNAUDITED_LEG) == 1
