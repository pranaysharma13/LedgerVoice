import pytest
from nessie_tools import AuditError, AuditLog, TransferStatus

S = TransferStatus


def make(audit, tid="LV-TRF-00000001", status=S.EXECUTING):
    return audit.create(tid, "cust", "acc-from", "acc-to", 50, status=status)


def test_create_and_happy_path_history():
    with AuditLog(":memory:") as audit:
        make(audit)
        audit.transition("LV-TRF-00000001", S.LEG1_DONE, withdrawal_id="w-1")
        rec = audit.transition("LV-TRF-00000001", S.COMPLETED, deposit_id="d-1")

        assert rec.status is S.COMPLETED
        assert (rec.withdrawal_id, rec.deposit_id, rec.reversal_id) == ("w-1", "d-1", None)
        history = [e.status for e in audit.events("LV-TRF-00000001")]
        assert history == [S.EXECUTING, S.LEG1_DONE, S.COMPLETED]


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
            audit.transition("LV-TRF-00000001", jump)
        assert audit.get("LV-TRF-00000001").status is start


def test_terminal_states_are_final():
    with AuditLog(":memory:") as audit:
        make(audit)
        audit.transition("LV-TRF-00000001", S.FAILED)
        with pytest.raises(AuditError):
            audit.transition("LV-TRF-00000001", S.LEG1_DONE)


def test_must_start_as_pending_or_executing():
    with AuditLog(":memory:") as audit, pytest.raises(AuditError):
        make(audit, status=S.COMPLETED)


def test_duplicate_id_is_refused():
    with AuditLog(":memory:") as audit:
        make(audit)
        with pytest.raises(AuditError):
            make(audit)


def test_non_whole_amount_is_refused():
    with AuditLog(":memory:") as audit, pytest.raises(ValueError):
        audit.create("LV-TRF-00000002", "cust", "a", "b", 12.5)


def test_unknown_id():
    with AuditLog(":memory:") as audit:
        assert audit.find("LV-TRF-nope") is None
        with pytest.raises(AuditError):
            audit.get("LV-TRF-nope")


def test_survives_reopening(tmp_path):
    path = tmp_path / "sub" / "audit.sqlite"  # parent folder is created
    with AuditLog(path) as audit:
        make(audit)
        audit.transition("LV-TRF-00000001", S.FAILED, detail="leg 1 rejected")
    with AuditLog(path) as audit:
        assert audit.get("LV-TRF-00000001").status is S.FAILED
        assert audit.events("LV-TRF-00000001")[-1].detail == "leg 1 rejected"


def test_records_filter_by_status():
    with AuditLog(":memory:") as audit:
        make(audit, "LV-TRF-00000001")
        make(audit, "LV-TRF-00000002", status=S.PENDING)
        assert [r.transfer_id for r in audit.records(S.PENDING)] == ["LV-TRF-00000002"]
        assert len(audit.records()) == 2
