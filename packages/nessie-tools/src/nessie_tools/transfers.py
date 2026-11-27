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

Concurrency (V1-01, decision D37): one lock per account, shared by every
TransferService in the process. Executing a transfer holds the locks of BOTH its
accounts (V3-01), taken in sorted id order so two transfers in opposite directions
can't deadlock. So the funds check (source) and the card cap (destination) can't be
passed by two transfers at once. Run ONE process per audit database; reconcile also
reports overdrawn accounts as a safety net.

Known limitation (R1-12, accepted): if the local audit write itself fails right after
a bank write (e.g. disk full), the exception propagates and the record stays in
EXECUTING or LEG1_DONE. reconcile reports it as stuck_in_progress.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from nessie_tools.audit import AuditLog, TransferRecord, TransferStatus, require_aware_clock
from nessie_tools.backend import BankBackend
from nessie_tools.balances import compute_balance
from nessie_tools.errors import (
    BankError,
    LedgerError,
    NotFoundError,
    is_ambiguous,
    is_temporary,
)
from nessie_tools.models import Account, AccountType, Deposit, Withdrawal
from nessie_tools.money import require_whole_dollars

S = TransferStatus

TRANSFER_ID_PREFIX = "LV-TRF-"
LEG1_MARKER = "(leg 1/2)"
LEG2_MARKER = "(leg 2/2)"
REVERSAL_MARKER = "(reversal of leg 1/2)"

# Pauses before each look-up after an ambiguous write: first look immediately,
# then wait a little longer each time (~3.5 s worst case).
DEFAULT_RECHECK_DELAYS: tuple[float, ...] = (0.0, 0.5, 1.0, 2.0)
DEFAULT_MAX_AGE = timedelta(minutes=2)  # D54: how long a proposal can wait for its "yes"
DETAIL_MAX = 200


# Process-wide: every TransferService instance shares these (V1-01).
_ACCOUNT_LOCKS: dict[str, threading.Lock] = {}
_ACCOUNT_LOCKS_GUARD = threading.Lock()


@contextmanager
def _account_locks(*account_ids: str) -> Iterator[None]:
    """Hold the lock of every given account, always acquired in sorted id order: any
    two transfers take shared locks in the same order, so they can never wait for each
    other in a cycle (no deadlock, V3-01)."""
    with _ACCOUNT_LOCKS_GUARD:
        locks = [_ACCOUNT_LOCKS.setdefault(a, threading.Lock()) for a in sorted(set(account_ids))]
    with ExitStack() as stack:
        for lock in locks:
            stack.enter_context(lock)
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


class TransferExpired(TransferRejected):
    """execute_pending refused a pending transfer older than `max_age` (D54) and marked
    it EXPIRED. A subclass so the caller can log the backstop expiry as such (R2-08)."""


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


AccountState = Literal["exists", "gone", "unknown"]


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
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_age: timedelta | None = DEFAULT_MAX_AGE,
    ) -> None:
        """`max_age`: a pending transfer older than this can no longer be executed
        (D54); None disables the check. `now` must be the same clock the AuditLog uses;
        it must return timezone-aware datetimes (checked here, R2-04)."""
        require_aware_clock(now, "TransferService")
        self._bank = backend
        self._audit = audit
        self._today = today
        self._new_id = new_id
        self._recheck_delays = tuple(recheck_delays)
        self._sleep = sleep
        self._now = now
        self._max_age = max_age

    @property
    def max_age(self) -> timedelta | None:
        """The confirmation window; pass it to reconcile(stale_after=...) (V2-04)."""
        return self._max_age

    # --- step 1: propose ---------------------------------------------------------
    def create_pending(
        self, customer_id: str, from_account_id: str, to_account_id: str, amount: int
    ) -> TransferRecord:
        """Run the pre-checks and record a PENDING transfer. No money moves.

        Raises TransferRejected if a check fails (nothing is recorded).
        """
        amount = self._require_amount(amount)
        src, dst = self._check_accounts(customer_id, from_account_id, to_account_id)
        self._check_funds(src, amount)
        self._check_card_payment(dst, amount)
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
        with _account_locks(record.from_account, record.to_account):
            record = self._require_pending(transfer_id, customer_id)  # re-read under the lock
            self._refuse_if_expired(record)
            try:
                src, dst = self._check_accounts(
                    record.customer_id, record.from_account, record.to_account
                )
                self._check_funds(src, record.amount)
                self._check_card_payment(dst, record.amount)
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

    def _refuse_if_expired(self, record: TransferRecord) -> None:
        """D54: a "yes" can only confirm a recent proposal. A stale pending transfer is
        marked EXPIRED, even if the caller (the conversation runtime) forgot to."""
        if self._max_age is None:
            return
        age = self._now() - datetime.fromisoformat(record.created_at)
        if age > self._max_age:
            self._audit.transition(
                record.transfer_id,
                S.EXPIRED,
                detail=f"expired: pending for {age.total_seconds():.0f}s "
                f"(limit {self._max_age.total_seconds():.0f}s)",
            )
            raise TransferExpired("this transfer request has expired; please ask again")

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
        # sure the destination still exists right before writing (V1-03). If we can't
        # tell, don't write: leg 2 is then definitely not written and leg 1 is
        # returned below (D40).
        desc2 = leg2_description(tid, src.last4)
        dst_state, dst_reason = self._account_state(dst.id)
        if dst_state == "gone":
            leg2 = _Outcome("not_written", reason="destination account no longer exists")
        elif dst_state == "unknown":
            leg2 = _Outcome(
                "not_written", reason=f"could not verify the destination account: {dst_reason}"
            )
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

        # compensation: leg 2 was definitely not written, so put the money back - but
        # only into a source account we know still exists (D40). Otherwise a human
        # has to decide where the money goes.
        src_state, src_reason = self._account_state(src.id)
        if src_state != "exists":
            why = (
                "source account no longer exists"
                if src_state == "gone"
                else f"could not verify the source account: {src_reason}"
            )
            return self._finish(
                tid,
                S.NEEDS_ATTENTION,
                f"leg 2 rejected: {leg2.reason}; {why}; reversal not written",
                src,
                dst,
            )
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

    def _account_state(self, account_id: str) -> tuple[AccountState, str]:
        """Does the account exist right now? "unknown" if the check itself failed.

        Callers must decide explicitly what "unknown" means, so a failed check can never
        be mistaken for "exists" (D40).
        """
        try:
            self._bank.get_account(account_id)
        except NotFoundError:
            return "gone", ""
        except BankError as exc:
            return "unknown", _clip(str(exc))
        return "exists", ""

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
            raise TransferRejected(
                f"account lookup failed: {exc}", retryable=is_temporary(exc)
            ) from None
        if src.customer_id != customer_id or dst.customer_id != customer_id:
            raise TransferRejected("both accounts must belong to the customer")
        return src, dst

    def _check_funds(self, src: Account, amount: int) -> None:
        try:
            available = compute_balance(self._bank, src.id, account=src).current
        except BankError as exc:
            raise TransferRejected(
                f"could not read the balance: {exc}", retryable=is_temporary(exc)
            ) from None
        if amount > available:
            raise TransferRejected(f"insufficient funds: ${available} available")

    def _check_card_payment(self, dst: Account, amount: int) -> None:
        """A payment into a credit card can't be more than the card owes (D57, D45).
        Checked when proposing and again at execution, like the funds check, because a
        payment may land in between (V2-05). Owed = minus the computed balance."""
        if dst.type is not AccountType.CREDIT_CARD:
            return
        try:
            owed = -compute_balance(self._bank, dst.id, account=dst).current
        except BankError as exc:
            raise TransferRejected(
                f"could not read what the card owes: {exc}", retryable=is_temporary(exc)
            ) from None
        card = f"credit card ending in {dst.last4}"
        if owed <= 0:
            raise TransferRejected(f"nothing is owed on the {card}, so there is nothing to pay")
        if amount > owed:
            raise TransferRejected(
                f"the {card} only owes ${owed:,}, so the most you can pay into it is ${owed:,}"
            )


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= DETAIL_MAX else text[: DETAIL_MAX - 1] + "…"
