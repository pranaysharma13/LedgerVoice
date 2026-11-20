"""SQLite audit log for transfers (decision D22).

This is the source of truth for which transfers exist and what state they are in.
Every leg written to the bank carries the transfer id in its description, so
reconcile.py can compare this log with the bank's records.

Two tables:
- transfers:       one row per transfer, current state
- transfer_events: append-only history of every state change (who-did-what-when)

State machine (invalid jumps are refused, so a bug can't silently skip a step):

    PENDING ──> EXECUTING ──> LEG1_DONE ──> COMPLETED
       │            │              ├──────> COMPENSATED
       │            │              └──────> NEEDS_ATTENTION
       │            ├──> FAILED
       │            └──> NEEDS_ATTENTION
       ├──> CANCELLED
       └──> EXPIRED

PENDING / CANCELLED / EXPIRED are used by the confirmation flow in M1d.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from nessie_tools.errors import BankError
from nessie_tools.money import require_whole_dollars


class TransferStatus(StrEnum):
    PENDING = "PENDING"
    EXECUTING = "EXECUTING"
    LEG1_DONE = "LEG1_DONE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    COMPENSATED = "COMPENSATED"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


S = TransferStatus
ALLOWED: dict[TransferStatus, frozenset[TransferStatus]] = {
    S.PENDING: frozenset({S.EXECUTING, S.CANCELLED, S.EXPIRED}),
    S.EXECUTING: frozenset({S.LEG1_DONE, S.FAILED, S.NEEDS_ATTENTION}),
    S.LEG1_DONE: frozenset({S.COMPLETED, S.COMPENSATED, S.NEEDS_ATTENTION}),
    S.COMPLETED: frozenset(),
    S.FAILED: frozenset(),
    S.COMPENSATED: frozenset(),
    S.NEEDS_ATTENTION: frozenset(),
    S.CANCELLED: frozenset(),
    S.EXPIRED: frozenset(),
}
TERMINAL = frozenset(s for s, nxt in ALLOWED.items() if not nxt)
INITIAL = frozenset({S.PENDING, S.EXECUTING})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transfers (
    transfer_id   TEXT PRIMARY KEY,
    customer_id   TEXT NOT NULL,
    from_account  TEXT NOT NULL,
    to_account    TEXT NOT NULL,
    amount        INTEGER NOT NULL CHECK (amount > 0),
    status        TEXT NOT NULL,
    withdrawal_id TEXT,
    deposit_id    TEXT,
    reversal_id   TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transfer_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_id TEXT NOT NULL REFERENCES transfers(transfer_id),
    status      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    at          TEXT NOT NULL
);
"""


class AuditError(BankError):
    """Invalid audit operation: unknown id, duplicate id, or a forbidden state jump."""


@dataclass(frozen=True)
class TransferRecord:
    transfer_id: str
    customer_id: str
    from_account: str
    to_account: str
    amount: int
    status: TransferStatus
    withdrawal_id: str | None
    deposit_id: str | None
    reversal_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TransferEvent:
    transfer_id: str
    status: TransferStatus
    detail: str
    at: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AuditLog:
    def __init__(self, path: str | Path, now: Callable[[], datetime] = _utc_now) -> None:
        """Open (or create) the audit database. Use ":memory:" in tests."""
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._now = now

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> AuditLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- writes ----------------------------------------------------------------
    def create(
        self,
        transfer_id: str,
        customer_id: str,
        from_account: str,
        to_account: str,
        amount: int,
        status: TransferStatus = TransferStatus.EXECUTING,
        detail: str = "",
    ) -> TransferRecord:
        if status not in INITIAL:
            raise AuditError(f"a transfer must start as PENDING or EXECUTING, not {status}")
        amount = require_whole_dollars(amount)
        ts = self._now().isoformat()
        try:
            with self._conn:  # one atomic transaction: both rows or neither
                self._conn.execute(
                    "INSERT INTO transfers (transfer_id, customer_id, from_account, to_account,"
                    " amount, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (transfer_id, customer_id, from_account, to_account, amount, status, ts, ts),
                )
                self._add_event(transfer_id, status, detail, ts)
        except sqlite3.IntegrityError:
            raise AuditError(f"transfer {transfer_id} already exists") from None
        return self.get(transfer_id)

    def transition(
        self,
        transfer_id: str,
        new_status: TransferStatus,
        *,
        detail: str = "",
        withdrawal_id: str | None = None,
        deposit_id: str | None = None,
        reversal_id: str | None = None,
    ) -> TransferRecord:
        current = self.get(transfer_id)
        if new_status not in ALLOWED[current.status]:
            raise AuditError(f"{transfer_id}: {current.status} -> {new_status} is not allowed")
        legs = {
            "withdrawal_id": withdrawal_id,
            "deposit_id": deposit_id,
            "reversal_id": reversal_id,
        }
        updates = {k: v for k, v in legs.items() if v is not None}
        ts = self._now().isoformat()
        sets = ", ".join(f"{column} = ?" for column in ("status", "updated_at", *updates))
        with self._conn:
            # The WHERE on the old status makes this a compare-and-set: if anything
            # changed the row since we read it, no row matches and we refuse.
            cur = self._conn.execute(
                f"UPDATE transfers SET {sets} WHERE transfer_id = ? AND status = ?",
                (new_status, ts, *updates.values(), transfer_id, current.status),
            )
            if cur.rowcount != 1:
                raise AuditError(f"{transfer_id}: concurrent update detected")
            self._add_event(transfer_id, new_status, detail, ts)
        return self.get(transfer_id)

    def _add_event(self, transfer_id: str, status: TransferStatus, detail: str, ts: str) -> None:
        self._conn.execute(
            "INSERT INTO transfer_events (transfer_id, status, detail, at) VALUES (?, ?, ?, ?)",
            (transfer_id, status, detail, ts),
        )

    # --- reads -----------------------------------------------------------------
    def get(self, transfer_id: str) -> TransferRecord:
        row = self._conn.execute(
            "SELECT * FROM transfers WHERE transfer_id = ?", (transfer_id,)
        ).fetchone()
        if row is None:
            raise AuditError(f"transfer {transfer_id} not found")
        return self._record(row)

    def find(self, transfer_id: str) -> TransferRecord | None:
        try:
            return self.get(transfer_id)
        except AuditError:
            return None

    def records(self, status: TransferStatus | None = None) -> list[TransferRecord]:
        if status is None:
            rows = self._conn.execute("SELECT * FROM transfers ORDER BY created_at, transfer_id")
        else:
            rows = self._conn.execute(
                "SELECT * FROM transfers WHERE status = ? ORDER BY created_at, transfer_id",
                (status,),
            )
        return [self._record(r) for r in rows.fetchall()]

    def events(self, transfer_id: str) -> list[TransferEvent]:
        rows = self._conn.execute(
            "SELECT transfer_id, status, detail, at FROM transfer_events"
            " WHERE transfer_id = ? ORDER BY id",
            (transfer_id,),
        ).fetchall()
        return [
            TransferEvent(r["transfer_id"], TransferStatus(r["status"]), r["detail"], r["at"])
            for r in rows
        ]

    @staticmethod
    def _record(row: sqlite3.Row) -> TransferRecord:
        return TransferRecord(
            transfer_id=row["transfer_id"],
            customer_id=row["customer_id"],
            from_account=row["from_account"],
            to_account=row["to_account"],
            amount=row["amount"],
            status=TransferStatus(row["status"]),
            withdrawal_id=row["withdrawal_id"],
            deposit_id=row["deposit_id"],
            reversal_id=row["reversal_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
