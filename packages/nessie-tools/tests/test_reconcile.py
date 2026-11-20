"""Reconciliation catches every way the audit log and the bank can disagree."""

from datetime import date

from nessie_tools import TransferStatus, reconcile

S = TransferStatus
DAY = date(2026, 11, 19)


def run(bank):
    return reconcile(bank.fake, bank.audit, [bank.checking.id, bank.savings.id])


def test_clean_history_has_no_violations(bank):
    bank.service.execute(bank.customer_id, bank.checking.id, bank.savings.id, 50)
    bank.fake.fail_next("create_deposit")
    bank.service.execute(bank.customer_id, bank.checking.id, bank.savings.id, 20)  # compensated
    bank.fake.fail_next("create_withdrawal")
    bank.service.execute(bank.customer_id, bank.checking.id, bank.savings.id, 10)  # failed

    report = run(bank)
    assert report.ok, report.violations
    assert report.transfers_checked == 3


def test_money_moved_without_audit_record_is_caught(bank):
    """The core safety check: a leg with a transfer id but no audit entry."""
    bank.fake.create_withdrawal(bank.checking.id, 75, DAY, "LV-TRF-deadbeef sneaky (leg 1/2)")
    report = run(bank)
    assert report.count("unaudited_leg") == 1
    assert report.violations[0].transfer_id == "LV-TRF-deadbeef"


def test_unrelated_transactions_are_ignored(bank):
    bank.fake.create_withdrawal(bank.checking.id, 40, DAY, "ATM withdrawal")
    assert run(bank).ok


def test_completed_record_without_legs_is_missing_legs(bank):
    a = bank.audit
    a.create("LV-TRF-ghost001", bank.customer_id, bank.checking.id, bank.savings.id, 30)
    a.transition("LV-TRF-ghost001", S.LEG1_DONE)
    a.transition("LV-TRF-ghost001", S.COMPLETED)
    assert run(bank).count("missing_leg") == 2


def test_wrong_amount_is_a_mismatch(bank):
    a, tid = bank.audit, "LV-TRF-mismatch"
    a.create(tid, bank.customer_id, bank.checking.id, bank.savings.id, 30)
    bank.fake.create_withdrawal(bank.checking.id, 300, DAY, f"{tid} x (leg 1/2)")
    bank.fake.create_deposit(bank.savings.id, 30, DAY, f"{tid} x (leg 2/2)")
    a.transition(tid, S.LEG1_DONE)
    a.transition(tid, S.COMPLETED)
    report = run(bank)
    assert report.count("leg_mismatch") == 1


def test_failed_record_with_a_leg_is_unexpected(bank):
    a, tid = bank.audit, "LV-TRF-failedleg"
    a.create(tid, bank.customer_id, bank.checking.id, bank.savings.id, 30)
    a.transition(tid, S.FAILED)
    bank.fake.create_withdrawal(bank.checking.id, 30, DAY, f"{tid} x (leg 1/2)")
    assert run(bank).count("unexpected_leg") == 1


def test_duplicate_leg_is_caught(bank):
    tid = bank.service.execute(bank.customer_id, bank.checking.id, bank.savings.id, 25).transfer_id
    bank.fake.create_deposit(bank.savings.id, 25, DAY, f"{tid} again (leg 2/2)")
    assert run(bank).count("duplicate_leg") == 1


def test_stuck_transfer_is_reported(bank):
    bank.audit.create("LV-TRF-stuck001", bank.customer_id, bank.checking.id, bank.savings.id, 5)
    assert run(bank).count("stuck_in_progress") == 1


def test_pending_transfer_with_no_legs_is_fine(bank):
    bank.audit.create(
        "LV-TRF-pend0001", bank.customer_id, bank.checking.id, bank.savings.id, 5, S.PENDING
    )
    assert run(bank).ok


def test_deleted_account_is_reported(bank):
    bank.service.execute(bank.customer_id, bank.checking.id, bank.savings.id, 5)
    bank.fake.delete_account(bank.savings.id)
    report = reconcile(bank.fake, bank.audit)
    assert report.count("account_missing") == 1
