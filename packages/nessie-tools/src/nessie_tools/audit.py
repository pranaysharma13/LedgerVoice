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
       │            │              └──────> NEEDS_ATTENTION ──> RESOLVED
       │            ├──> FAILED
       │            └──> NEEDS_ATTENTION ──> RESOLVED
       ├──> CANCELLED
       └──> EXPIRED

PENDING / CANCELLED / EXPIRED are used by the confirmation flow (M1d).
RESOLVED records that a human checked and fixed a NEEDS_ATTENTION transfer. It is only
reachable through resolve(), which requires a note AND a snapshot of the leg ids the
human saw in the bank, so reconcile can flag any leg that lands later (V1-02, D38).

Safe to share between threads: one connection guarded by a lock (R1-08).
The schema is versioned with PRAGMA user_version and migrated on open; each migration
step runs in its own IMMEDIATE transaction, so a failed step leaves no trace (V1-05).
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from nessie_tools.errors import LedgerError
from nessie_tools.money import require_whole_dollars

TRANSFER_ID_RE = re.compile(r"^LV-TRF-[0-9a-f]{8}$")


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
    RESOLVED = "RESOLVED"


S = TransferStatus
ALLOWED: dict[TransferStatus, frozenset[TransferStatus]] = {
    S.PENDING: frozenset({S.EXECUTING, S.CANCELLED, S.EXPIRED}),
    S.EXECUTING: frozenset({S.LEG1_DONE, S.FAILED, S.NEEDS_ATTENTION}),
    S.LEG1_DONE: frozenset({S.COMPLETED, S.COMPENSATED, S.NEEDS_ATTENTION}),
    S.COMPLETED: frozenset(),
    S.FAILED: frozenset(),
    S.COMPENSATED: frozenset(),
    S.NEEDS_ATTENTION: frozenset({S.RESOLVED}),
    S.CANCELLED: frozenset(),
    S.EXPIRED: frozenset(),
    S.RESOLVED: frozenset(),
}
TERMINAL = frozenset(s for s, nxt in ALLOWED.items() if not nxt)
INITIAL = frozenset({S.PENDING, S.EXECUTING})

# Ordered migrations: entry i brings the schema from version i to version i + 1.
# Each entry is a list of single SQL statements, applied in one transaction together
# with the user_version bump. Never edit an entry once released; add a new one instead.
_MIGRATIONS: list[list[str]] = [
    # v0 -> v1: initial schema
    [
        """CREATE TABLE IF NOT EXISTS transfers (
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
        )""",
        """CREATE TABLE IF NOT EXISTS transfer_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            transfer_id TEXT NOT NULL REFERENCES transfers(transfer_id),
            status      TEXT NOT NULL,
            detail      TEXT NOT NULL DEFAULT '',
            at          TEXT NOT NULL
        )""",
    ],
    # v1 -> v2: index for per-customer queries (reconcile scope, M1d lookups)
    ["CREATE INDEX IF NOT EXISTS idx_transfers_customer ON transfers (customer_id, status)"],
    # v2 -> v3: snapshot of the leg ids a human saw when resolving (V1-02), JSON list
    ["ALTER TABLE transfers ADD COLUMN resolution_leg_ids TEXT"],
    # v3 -> v4: every safety decision of the conversation layer (M1d, D55), including
    # ones that never create a transfer (rejected proposals, messages that weren't a yes)
    [
        """CREATE TABLE IF NOT EXISTS safety_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            at          TEXT NOT NULL,
            customer_id TEXT NOT NULL,
            transfer_id TEXT,
            kind        TEXT NOT NULL CHECK (kind <> ''),
            detail      TEXT NOT NULL DEFAULT ''
        )""",
        "CREATE INDEX IF NOT EXISTS idx_safety_events_customer ON safety_events (customer_id, id)",
    ],
]
SCHEMA_VERSION = len(_MIGRATIONS)


class AuditError(LedgerError):
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
    resolution_leg_ids: tuple[str, ...] | None = None  # set only by resolve()


@dataclass(frozen=True)
class SafetyEvent:
    """One decision of the conversation safety layer (M1d): proposed, confirmed,
    cancelled, expired, rejected, ... `kind` is defined by the caller."""

    customer_id: str
    kind: str
    transfer_id: str | None
    detail: str
    at: str


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
        # check_same_thread=False + our own lock: usable from worker threads
        # (e.g. asyncio.to_thread under Pipecat), one statement group at a time.
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._now = now
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._migrate()

    def _migrate(self) -> None:
        version = self._read_version()
        if version > SCHEMA_VERSION:
            raise AuditError(
                f"audit database schema v{version} is newer than this code (v{SCHEMA_VERSION})"
            )
        for target in range(version, SCHEMA_VERSION):
            # One IMMEDIATE transaction per step: the statements and the version bump
            # commit together or not at all. Re-reading the version inside the
            # transaction means a second process that already applied it is skipped.
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if self._read_version() > target:
                    self._conn.rollback()
                    continue
                for statement in _MIGRATIONS[target]:
                    self._conn.execute(statement)
                self._conn.execute(f"PRAGMA user_version = {target + 1}")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _read_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    @property
    def schema_version(self) -> int:
        with self._lock:
            return self._read_version()

    def close(self) -> None:
        with self._lock:
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
        *,
        status: TransferStatus,
        detail: str = "",
    ) -> TransferRecord:
        """Create a record. `status` is required on purpose (R1-24): PENDING or EXECUTING."""
        if not TRANSFER_ID_RE.match(transfer_id):
            raise AuditError(f"invalid transfer id {transfer_id!r} (expected LV-TRF-<8 hex>)")
        if status not in INITIAL:
            raise AuditError(f"a transfer must start as PENDING or EXECUTING, not {status}")
        amount = require_whole_dollars(amount)
        ts = self._now().isoformat()
        try:
            with self._lock, self._conn:  # one atomic transaction: both rows or neither
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
        if new_status is S.RESOLVED:
            raise AuditError(f"{transfer_id}: use resolve() to record a resolution")
        return self._transition(
            transfer_id,
            new_status,
            detail,
            {"withdrawal_id": withdrawal_id, "deposit_id": deposit_id, "reversal_id": reversal_id},
        )

    def resolve(self, transfer_id: str, note: str, seen_leg_ids: list[str]) -> TransferRecord:
        """NEEDS_ATTENTION -> RESOLVED, after a human checked the bank.

        `note` says what was done; `seen_leg_ids` lists every bank record id the human
        saw for this transfer (possibly empty). Reconcile flags any other leg that
        appears later for this transfer as leg_after_resolution.
        """
        if not note.strip():
            raise AuditError(f"{transfer_id}: resolving requires a note saying what was done")
        snapshot = json.dumps(sorted(set(seen_leg_ids)))
        return self._transition(transfer_id, S.RESOLVED, note, {"resolution_leg_ids": snapshot})

    def _transition(
        self,
        transfer_id: str,
        new_status: TransferStatus,
        detail: str,
        fields: dict[str, str | None],
    ) -> TransferRecord:
        current = self.get(transfer_id)
        if new_status not in ALLOWED[current.status]:
            raise AuditError(f"{transfer_id}: {current.status} -> {new_status} is not allowed")
        updates = {k: v for k, v in fields.items() if v is not None}
        ts = self._now().isoformat()
        sets = ", ".join(f"{column} = ?" for column in ("status", "updated_at", *updates))
        with self._lock, self._conn:
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
        with self._lock:
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

    def records(
        self,
        status: TransferStatus | None = None,
        customer_ids: set[str] | frozenset[str] | None = None,
    ) -> list[TransferRecord]:
        """Records, optionally filtered by status and/or customers (filtered in SQL)."""
        where: list[str] = []
        params: list[str] = []
        if status is not None:
            where.append("status = ?")
            params.append(status)
        if customer_ids is not None:
            if not customer_ids:
                return []
            ids = sorted(customer_ids)
            where.append(f"customer_id IN ({', '.join('?' for _ in ids)})")
            params.extend(ids)
        sql = "SELECT * FROM transfers"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at, transfer_id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._record(r) for r in rows]

    def log_event(
        self, customer_id: str, kind: str, *, transfer_id: str | None = None, detail: str = ""
    ) -> None:
        """Record a safety decision (D55). Never raises for an unknown transfer id: the
        log must also capture things that never became a transfer."""
        if not kind:
            raise AuditError("a safety event needs a kind")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO safety_events (at, customer_id, transfer_id, kind, detail)"
                " VALUES (?, ?, ?, ?, ?)",
                (self._now().isoformat(), customer_id, transfer_id, kind, detail),
            )

    def safety_events(self, customer_id: str | None = None) -> list[SafetyEvent]:
        sql = "SELECT customer_id, kind, transfer_id, detail, at FROM safety_events"
        params: tuple[str, ...] = ()
        if customer_id is not None:
            sql += " WHERE customer_id = ?"
            params = (customer_id,)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY id", params).fetchall()
        return [
            SafetyEvent(r["customer_id"], r["kind"], r["transfer_id"], r["detail"], r["at"])
            for r in rows
        ]

    def events(self, transfer_id: str) -> list[TransferEvent]:
        with self._lock:
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
            resolution_leg_ids=(
                tuple(json.loads(row["resolution_leg_ids"]))
                if row["resolution_leg_ids"] is not None
                else None
            ),
        )
