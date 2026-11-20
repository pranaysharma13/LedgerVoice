import sqlite3
import threading

import pytest
from nessie_tools import AuditError, AuditLog, LedgerError, TransferStatus
from nessie_tools.audit import SCHEMA_VERSION

S = TransferStatus
TID = "LV-TRF-00000001"


def make(audit, tid=TID, status=S.EXECUTING):
    return audit.create(tid, "cust", "acc-from", "acc-to", 50, status=status)


def test_create_and_happy_path_history():
    with AuditLog(":memory:") as audit:
        make(audit)
        audit.transition(TID, S.LEG1_DONE, withdrawal_id="w-1")
        rec = audit.transition(TID, S.COMPLETED, deposit_id="d-1")

        assert rec.status is S.COMPLETED
        assert (rec.withdrawal_id, rec.deposit_id, rec.reversal_id) == ("w-1", "d-1", None)
        assert [e.status for e in audit.events(TID)] == [S.EXECUTING, S.LEG1_DONE, S.COMPLETED]


@pytest.mark.parametrize(
    ("start", "jump"),
    [
        (S.EXECUTING, S.COMPLETED),  # can't skip leg 1
        (S.PENDING, S.LEG1_DONE),  # can't skip execution
        (S.EXECUTING, S.COMPENSATED),  # nothing to compensate before leg 1
        (S.EXECUTING, S.RESOLVED),  # only NEEDS_ATTENTION can be resolved
    ],
)
def test_forbidden_jumps_are_refused(start, jump):
    with AuditLog(":memory:") as audit:
        make(audit, status=start)
        with pytest.raises(AuditError):
            audit.transition(TID, jump, detail="x")
        assert audit.get(TID).status is start


def test_terminal_states_are_final():
    with AuditLog(":memory:") as audit:
        make(audit)
        audit.transition(TID, S.FAILED)
        with pytest.raises(AuditError):
            audit.transition(TID, S.LEG1_DONE)


def test_needs_attention_can_be_resolved_with_a_note():
    with AuditLog(":memory:") as audit:
        make(audit)
        audit.transition(TID, S.NEEDS_ATTENTION, detail="leg 1 outcome unknown")
        with pytest.raises(AuditError, match="note"):
            audit.transition(TID, S.RESOLVED, detail="   ")
        rec = audit.transition(TID, S.RESOLVED, detail="checked bank: withdrawal landed, fixed")
        assert rec.status is S.RESOLVED
        with pytest.raises(AuditError):
            audit.transition(TID, S.NEEDS_ATTENTION)


def test_status_is_required_and_must_be_initial():
    with AuditLog(":memory:") as audit:
        with pytest.raises(TypeError):
            audit.create(TID, "cust", "a", "b", 50)  # no status: refused (R1-24)
        with pytest.raises(AuditError):
            make(audit, status=S.COMPLETED)


@pytest.mark.parametrize("bad", ["confirm 42", "LV-TRF-hallucinated", "LV-TRF-0000001", "x"])
def test_transfer_id_format_is_enforced(bad):
    with AuditLog(":memory:") as audit, pytest.raises(AuditError, match="invalid transfer id"):
        make(audit, tid=bad)


def test_duplicate_id_is_refused_and_leaves_no_orphan_event():
    with AuditLog(":memory:") as audit:
        make(audit)
        with pytest.raises(AuditError):
            make(audit)
        assert len(audit.events(TID)) == 1


def test_non_whole_amount_is_refused():
    with AuditLog(":memory:") as audit, pytest.raises(ValueError):
        audit.create(TID, "cust", "a", "b", 12.5, status=S.PENDING)


def test_unknown_id():
    with AuditLog(":memory:") as audit:
        assert audit.find("LV-TRF-ffffffff") is None
        with pytest.raises(AuditError):
            audit.get("LV-TRF-ffffffff")


def test_audit_errors_are_ledger_errors_not_bank_errors():
    assert issubclass(AuditError, LedgerError)


def test_survives_reopening_and_is_versioned(tmp_path):
    path = tmp_path / "sub" / "audit.sqlite"  # parent folder is created
    with AuditLog(path) as audit:
        assert audit.schema_version == SCHEMA_VERSION
        make(audit)
        audit.transition(TID, S.FAILED, detail="leg 1 rejected")
    with AuditLog(path) as audit:
        assert audit.get(TID).status is S.FAILED
        assert audit.events(TID)[-1].detail == "leg 1 rejected"


def test_old_unversioned_database_is_migrated(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE transfers (transfer_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL,"
        " from_account TEXT NOT NULL, to_account TEXT NOT NULL, amount INTEGER NOT NULL,"
        " status TEXT NOT NULL, withdrawal_id TEXT, deposit_id TEXT, reversal_id TEXT,"
        " created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
    )
    conn.close()
    with AuditLog(path) as audit:
        assert audit.schema_version == SCHEMA_VERSION
        make(audit)  # the events table was added by the migration


def test_newer_database_is_refused(tmp_path):
    path = tmp_path / "future.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(AuditError, match="newer"):
        AuditLog(path)


def test_two_connections_see_each_others_changes(tmp_path):
    path = tmp_path / "shared.sqlite"
    with AuditLog(path) as a, AuditLog(path) as b:
        make(a)
        b.transition(TID, S.LEG1_DONE)
        assert a.get(TID).status is S.LEG1_DONE
        with pytest.raises(AuditError):  # a's view is refreshed: EXECUTING -> FAILED refused
            a.transition(TID, S.FAILED)


def test_usable_from_another_thread():
    with AuditLog(":memory:") as audit:
        errors = []

        def work():
            try:
                make(audit)
            except Exception as exc:  # pragma: no cover - the assertion reports it
                errors.append(exc)

        t = threading.Thread(target=work)
        t.start()
        t.join()
        assert errors == [] and audit.get(TID).status is S.EXECUTING


def test_records_filter_by_status_and_customer():
    with AuditLog(":memory:") as audit:
        make(audit, "LV-TRF-00000001")
        make(audit, "LV-TRF-00000002", status=S.PENDING)
        audit.create("LV-TRF-00000003", "other", "x", "y", 5, status=S.PENDING)
        assert [r.transfer_id for r in audit.records(S.PENDING, customer_ids={"cust"})] == [
            "LV-TRF-00000002"
        ]
        assert len(audit.records()) == 3
