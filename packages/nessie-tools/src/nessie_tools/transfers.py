"""Transfers between a customer's own accounts, run as a saga (decision D15).

Two-step API (R1-01, decision D34): there is NO way to move money in one call.
    create_pending(...)                       pre-checks, then records a PENDING transfer
    execute_pending(id, customer_id=...)      runs an existing PENDING transfer, nothing else
    cancel_pending(id, customer_id=..., ...)  PENDING -> CANCELLED or EXPIRED
Both take the customer id, so a session can only act on its own transfers (V1-06).
The confirmation flow (M1d) sits between the two steps: only a user's explicit yes,
checked by code, leads to execute_pending. An unknown or invented id is rejected.

Nessie transfers have no destination field, so a transfer is two writes:
    leg 1: withdrawal from the source account
    leg 2: deposit into the destination account
Every leg's description starts with the transfer id (e.g. "LV-TRF-3f9a1c2e ..."),
which is how reconcile.py ties bank records back to the audit log.

A saga is a sequence of steps where each step that can fail after an earlier step
succeeded has a COMPENSATING action that undoes the earlier step:
- leg 1 definitely not written   -> nothing moved: FAILED
- leg 2 definitely not written   -> reversing deposit into the source: COMPENSATED
- reversal not written           -> NEEDS_ATTENTION (a human must look)

Ambiguous outcomes (R1-02, decision D33): a timeout or 5xx can happen while the bank
is still processing the write, and the write can land AFTER we look for it. So:
- a rejection (4xx) means "definitely not written";
- an ambiguous error triggers a few re-checks with short pauses, looking the leg up
  by its exact description;
- if it is still not found, the outcome is UNKNOWN and the transfer goes to
  NEEDS_ATTENTION. We never conclude FAILED, and never compensate, on a guess.

Concurrency (V1-01, decision D37): one lock per source account, shared by every
TransferService in the process, so the funds check and the writes for an account never
interleave. Run ONE process per audit database; reconcile also reports overdrawn
accounts as a safety net.

Known limitation (R1-12, accepted): if the local audit write itself fails right after
a bank write (e.g. disk full), the exception propagates and the record stays in
EXECUTING or LEG1_DONE. reconcile reports it as stuck_in_progress.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Literal

from nessie_tools.audit import AuditLog, TransferRecord, TransferStatus
from nessie_tools.backend import BankBackend
from nessie_tools.balances import compute_balance
from nessie_tools.errors import BankError, LedgerError, NotFoundError, is_ambiguous
from nessie_tools.models import Account, Deposit, Withdrawal
from nessie_tools.money import require_whole_dollars

S = TransferStatus

TRANSFER_ID_PREFIX = "LV-TRF-"
LEG1_MARKER = "(leg 1/2)"
LEG2_MARKER = "(leg 2/2)"
REVERSAL_MARKER = "(reversal of leg 1/2)"

# Pauses before each look-up after an ambiguous write: first look immediately,
# then wait a little longer each time (~3.5 s worst case).
DEFAULT_RECHECK_DELAYS: tuple[float, ...] = (0.0, 0.5, 1.0, 2.0)
DETAIL_MAX = 200


# Process-wide: every TransferService instance shares these (V1-01).
_ACCOUNT_LOCKS: dict[str, threading.Lock] = {}
_ACCOUNT_LOCKS_GUARD = threading.Lock()


@contextmanager
def _account_lock(account_id: str) -> Iterator[None]:
    with _ACCOUNT_LOCKS_GUARD:
        lock = _ACCOUNT_LOCKS.setdefault(account_id, threading.Lock())
    with lock:
        yield


def new_transfer_id() -> str:
    return f"{TRANSFER_ID_PREFIX}{secrets.token_hex(4)}"


def leg1_description(transfer_id: str, to_last4: str) -> str:
    return f"{transfer_id} transfer to acct ending {to_last4} {LEG1_MARKER}"


def leg2_description(transfer_id: str, from_last4: str) -> str:
    return f"{transfer_id} transfer from acct ending {from_last4} {LEG2_MARKER}"


def reversal_description(transfer_id: str) -> str:
    return f"{transfer_id} {REVERSAL_MARKER}"


class TransferRejected(LedgerError):
    """A check failed before any money moved. The bank was not written to.

    retryable=True means the check could not be completed (e.g. the bank was briefly
    unreachable), not that the answer was no. At execution time such a transfer stays
    PENDING, so the caller can try again without asking the user twice (V1-04).
    """

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class TransferResult:
    transfer_id: str
    status: TransferStatus
    amount: int
    from_last4: str
    to_last4: str
    message: str  # short, user-facing, written to be spoken aloud

    @property
    def succeeded(self) -> bool:
        return self.status is TransferStatus.COMPLETED


@dataclass(frozen=True)
class _Outcome:
    """Result of trying to write one leg."""

    kind: Literal["written", "not_written", "unknown"]
    leg: Withdrawal | Deposit | None = None
    reason: str = ""


class TransferService:
    def __init__(
        self,
        backend: BankBackend,
        audit: AuditLog,
        today: Callable[[], date] = date.today,
        new_id: Callable[[], str] = new_transfer_id,
        recheck_delays: Sequence[float] = DEFAULT_RECHECK_DELAYS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._bank = backend
        self._audit = audit
        self._today = today
        self._new_id = new_id
        self._recheck_delays = tuple(recheck_delays)
        self._sleep = sleep

    # --- step 1: propose ---------------------------------------------------------
    def create_pending(
        self, customer_id: str, from_account_id: str, to_account_id: str, amount: int
    ) -> TransferRecord:
        """Run the pre-checks and record a PENDING transfer. No money moves.

        Raises TransferRejected if a check fails (nothing is recorded).
        """
        amount = self._require_amount(amount)
        src, _dst = self._check_accounts(customer_id, from_account_id, to_account_id)
        self._check_funds(src, amount)
        return self._audit.create(
            self._new_id(), customer_id, from_account_id, to_account_id, amount, status=S.PENDING
        )

    def cancel_pending(
        self, transfer_id: str, *, customer_id: str, expired: bool = False, reason: str = ""
    ) -> TransferRecord:
        """PENDING -> CANCELLED (or EXPIRED). Raises TransferRejected if not PENDING
        or not this customer's transfer."""
        record = self._require_pending(transfer_id, customer_id)
        status = S.EXPIRED if expired else S.CANCELLED
        return self._audit.transition(record.transfer_id, status, detail=_clip(reason))

    # --- step 2: execute ---------------------------------------------------------
    def execute_pending(self, transfer_id: str, *, customer_id: str) -> TransferResult:
        """Run an existing PENDING transfer belonging to `customer_id`. Anything else is
        rejected.

        Accounts and funds are checked again, because things may have changed since
        the transfer was proposed. If a check says no, the transfer is CANCELLED and
        TransferRejected is raised. If a check could not be completed (retryable), the
        transfer stays PENDING and TransferRejected(retryable=True) is raised.
        """
        record = self._require_pending(transfer_id, customer_id)
        with _account_lock(record.from_account):
            record = self._require_pending(transfer_id, customer_id)  # re-read under the lock
            try:
                src, dst = self._check_accounts(
                    record.customer_id, record.from_account, record.to_account
                )
                self._check_funds(src, record.amount)
            except TransferRejected as exc:
                if not exc.retryable:
                    self._audit.transition(
                        transfer_id,
                        S.CANCELLED,
                        detail=_clip(f"checks failed at execution: {exc}"),
                    )
                raise
            self._audit.transition(transfer_id, S.EXECUTING, detail="confirmed; executing")
            return self._run_legs(record, src, dst)

    # --- the saga ------------------------------------------------------------------
    def _run_legs(self, record: TransferRecord, src: Account, dst: Account) -> TransferResult:
        tid, amount, when = record.transfer_id, record.amount, self._today()

        # leg 1: withdrawal
        desc1 = leg1_description(tid, dst.last4)
        leg1 = self._write_leg(
            lambda: self._bank.create_withdrawal(src.id, amount, when, desc1),
            lambda: self._bank.list_withdrawals(src.id),
            desc1,
            amount,
        )
        if leg1.kind == "not_written":
            return self._finish(tid, S.FAILED, f"leg 1 rejected: {leg1.reason}", src, dst)
        if leg1.kind == "unknown" or leg1.leg is None:
            return self._finish(
                tid, S.NEEDS_ATTENTION, f"leg 1 outcome unknown: {leg1.reason}", src, dst
            )
        self._audit.transition(
            tid, S.LEG1_DONE, detail="withdrawal written", withdrawal_id=leg1.leg.id
        )

        # leg 2: deposit. The bank accepts deposits to deleted accounts (D36), so make
        # sure the destination still exists right before writing (V1-03).
        desc2 = leg2_description(tid, src.last4)
        if self._account_gone(dst.id):
            leg2 = _Outcome("not_written", reason="destination account no longer exists")
        else:
            leg2 = self._write_leg(
                lambda: self._bank.create_deposit(dst.id, amount, when, desc2),
                lambda: self._bank.list_deposits(dst.id),
                desc2,
                amount,
            )
        if leg2.kind == "written" and leg2.leg is not None:
            return self._finish(
                tid, S.COMPLETED, "deposit written", src, dst, deposit_id=leg2.leg.id
            )
        if leg2.kind == "unknown":
            # Leg 2 may still land: compensating now could create money. Stop here.
            return self._finish(
                tid, S.NEEDS_ATTENTION, f"leg 2 outcome unknown: {leg2.reason}", src, dst
            )

        # compensation: leg 2 was definitely not written, so put the money back
        desc_r = reversal_description(tid)
        reversal = self._write_leg(
            lambda: self._bank.create_deposit(src.id, amount, when, desc_r),
            lambda: self._bank.list_deposits(src.id),
            desc_r,
            amount,
        )
        if reversal.kind == "written" and reversal.leg is not None:
            return self._finish(
                tid,
                S.COMPENSATED,
                f"leg 2 rejected: {leg2.reason}; reversal written",
                src,
                dst,
                reversal_id=reversal.leg.id,
            )
        return self._finish(
            tid,
            S.NEEDS_ATTENTION,
            f"leg 2 rejected: {leg2.reason}; reversal {reversal.kind}: {reversal.reason}",
            src,
            dst,
        )

    def _write_leg(
        self,
        write: Callable[[], Withdrawal | Deposit],
        list_existing: Callable[[], list[Withdrawal] | list[Deposit]],
        description: str,
        amount: int,
    ) -> _Outcome:
        """Write one leg and classify the outcome as written / not_written / unknown."""
        try:
            return _Outcome("written", write())
        except BankError as exc:
            if not is_ambiguous(exc):
                return _Outcome("not_written", reason=_clip(str(exc)))
            first_error = _clip(str(exc))
        # Ambiguous: the write may have landed, or may still land. Look a few times.
        last_check_error = ""
        for delay in self._recheck_delays:
            if delay:
                self._sleep(delay)
            try:
                for leg in list_existing():
                    if leg.description == description and leg.amount == amount:
                        return _Outcome("written", leg)
            except BankError as check_exc:
                last_check_error = _clip(str(check_exc))
        note = f"{first_error}; not found after {len(self._recheck_delays)} checks"
        if last_check_error:
            note += f" (last check failed: {last_check_error})"
        return _Outcome("unknown", reason=_clip(note))

    def _finish(
        self,
        tid: str,
        status: TransferStatus,
        detail: str,
        src: Account,
        dst: Account,
        *,
        deposit_id: str | None = None,
        reversal_id: str | None = None,
    ) -> TransferResult:
        record = self._audit.transition(
            tid, status, detail=_clip(detail), deposit_id=deposit_id, reversal_id=reversal_id
        )
        amount = record.amount
        frm, to = f"account ending in {src.last4}", f"account ending in {dst.last4}"
        messages = {
            S.COMPLETED: f"Done. I moved ${amount} from your {frm} to your {to}.",
            S.FAILED: "The transfer didn't go through, so no money moved.",
            S.COMPENSATED: (
                f"The transfer failed partway, so the ${amount} was returned to your {frm}."
            ),
            S.NEEDS_ATTENTION: (
                "Something went wrong and I can't confirm whether the money moved. "
                f"It needs a manual check. The reference is {tid}."
            ),
        }
        return TransferResult(tid, status, amount, src.last4, dst.last4, messages[status])

    # --- checks ------------------------------------------------------------------
    @staticmethod
    def _require_amount(amount: int) -> int:
        try:
            return require_whole_dollars(amount)
        except BankError as exc:
            raise TransferRejected(str(exc)) from None

    def _require_pending(self, transfer_id: str, customer_id: str) -> TransferRecord:
        record = self._audit.find(transfer_id)
        if record is None or record.customer_id != customer_id:
            # Same message either way: don't reveal other customers' transfer ids.
            raise TransferRejected(f"no transfer {transfer_id!r} is waiting for confirmation")
        if record.status is not S.PENDING:
            raise TransferRejected(f"{transfer_id} is {record.status}, not PENDING")
        return record

    def _account_gone(self, account_id: str) -> bool:
        """True only if the bank says the account doesn't exist (a failed check is not proof)."""
        try:
            self._bank.get_account(account_id)
        except NotFoundError:
            return True
        except BankError:
            return False
        return False

    def _check_accounts(
        self, customer_id: str, from_account_id: str, to_account_id: str
    ) -> tuple[Account, Account]:
        if from_account_id == to_account_id:
            raise TransferRejected("source and destination are the same account")
        try:
            src = self._bank.get_account(from_account_id)
            dst = self._bank.get_account(to_account_id)
        except NotFoundError as exc:
            raise TransferRejected(f"account not found: {exc}") from None
        except BankError as exc:
            raise TransferRejected(f"account lookup failed: {exc}", retryable=True) from None
        if src.customer_id != customer_id or dst.customer_id != customer_id:
            raise TransferRejected("both accounts must belong to the customer")
        return src, dst

    def _check_funds(self, src: Account, amount: int) -> None:
        try:
            available = compute_balance(self._bank, src.id, account=src).current
        except BankError as exc:
            raise TransferRejected(f"could not read the balance: {exc}", retryable=True) from None
        if amount > available:
            raise TransferRejected(f"insufficient funds: ${available} available")


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= DETAIL_MAX else text[: DETAIL_MAX - 1] + "…"
