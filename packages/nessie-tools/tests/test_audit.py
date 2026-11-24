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


def test_needs_attention_is_resolved_only_via_resolve():
    with AuditLog(":memory:") as audit:
        make(audit)
        audit.transition(TID, S.NEEDS_ATTENTION, detail="leg 1 outcome unknown")
        with pytest.raises(AuditError, match="resolve"):
            audit.transition(TID, S.RESOLVED, detail="sneaky")
        with pytest.raises(AuditError, match="note"):
            audit.resolve(TID, "   ", [])
        rec = audit.resolve(TID, "checked bank: one withdrawal, refunded", ["w-2", "w-1"])
        assert rec.status is S.RESOLVED
        assert rec.resolution_leg_ids == ("w-1", "w-2")
        with pytest.raises(AuditError):
            audit.transition(TID, S.NEEDS_ATTENTION)


def test_only_needs_attention_can_be_resolved():
    with AuditLog(":memory:") as audit:
        make(audit)
        with pytest.raises(AuditError):
            audit.resolve(TID, "note", [])


def test_status_is_required_and_must_be_initial():
    with AuditLog(":memory:") as audit:
        with pytest.raises(TypeError):
            audit.create(TID, "cust", "a", "b", 50)  # type: ignore[call-arg]  # no status: refused (R1-24)
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
        audit.create(TID, "cust", "a", "b", 12.5, status=S.PENDING)  # type: ignore[arg-type]  # deliberate


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


def test_failed_migration_step_leaves_no_trace(tmp_path, monkeypatch):
    """V1-05: a step and its version bump commit together or not at all."""
    from nessie_tools import audit as audit_module

    path = tmp_path / "atomic.sqlite"
    AuditLog(path).close()  # fully migrated
    bad_step = ["ALTER TABLE transfers ADD COLUMN extra TEXT", "THIS IS NOT SQL"]
    monkeypatch.setattr(audit_module, "_MIGRATIONS", [*audit_module._MIGRATIONS, bad_step])
    monkeypatch.setattr(audit_module, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
    with pytest.raises(sqlite3.Error):
        AuditLog(path)
    conn = sqlite3.connect(path)
    columns = [row[1] for row in conn.execute("PRAGMA table_info(transfers)")]
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert "extra" not in columns  # the first statement was rolled back
    assert version == SCHEMA_VERSION  # and the version was not bumped


def test_v2_database_upgrades_to_v3_keeping_data(tmp_path, monkeypatch):
    from nessie_tools import audit as audit_module

    path = tmp_path / "v2.sqlite"
    monkeypatch.setattr(audit_module, "_MIGRATIONS", audit_module._MIGRATIONS[:2])
    monkeypatch.setattr(audit_module, "SCHEMA_VERSION", 2)
    AuditLog(path).close()  # a v2 database, as an older release left it
    monkeypatch.undo()
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO transfers (transfer_id, customer_id, from_account, to_account, amount,"
        " status, created_at, updated_at) VALUES (?, 'cust', 'a', 'b', 50, 'EXECUTING', 't', 't')",
        (TID,),
    )
    conn.commit()
    conn.close()
    with AuditLog(path) as new:
        assert new.schema_version == SCHEMA_VERSION
        assert new.get(TID).resolution_leg_ids is None
        new.transition(TID, S.NEEDS_ATTENTION, detail="x")
        assert new.resolve(TID, "fixed", ["w-1"]).resolution_leg_ids == ("w-1",)


def test_records_with_an_empty_customer_scope_is_empty():
    with AuditLog(":memory:") as audit:
        make(audit)
        assert audit.records(customer_ids=set()) == []


def test_safety_events_are_logged_in_order_and_filtered_by_customer():
    with AuditLog(":memory:") as audit:
        audit.log_event("cust-1", "proposed", transfer_id=TID, detail="$50")
        audit.log_event("cust-2", "rejected", detail="over the limit")  # no transfer at all
        audit.log_event("cust-1", "confirmed", transfer_id=TID)
        assert [e.kind for e in audit.safety_events("cust-1")] == ["proposed", "confirmed"]
        assert [e.kind for e in audit.safety_events()] == ["proposed", "rejected", "confirmed"]
        rejected = audit.safety_events("cust-2")[0]
        assert (rejected.transfer_id, rejected.detail) == (None, "over the limit")
        with pytest.raises(AuditError):
            audit.log_event("cust-1", "")


def test_v3_database_upgrades_to_v4_keeping_transfers(tmp_path, monkeypatch):
    from nessie_tools import audit as audit_module

    path = tmp_path / "v3.sqlite"
    monkeypatch.setattr(audit_module, "_MIGRATIONS", audit_module._MIGRATIONS[:3])
    monkeypatch.setattr(audit_module, "SCHEMA_VERSION", 3)
    with AuditLog(path) as old:
        make(old)
    monkeypatch.undo()
    with AuditLog(path) as new:
        assert new.schema_version == SCHEMA_VERSION == 4
        assert new.get(TID).status is S.EXECUTING
        new.log_event("cust", "proposed", transfer_id=TID)
        assert len(new.safety_events()) == 1
