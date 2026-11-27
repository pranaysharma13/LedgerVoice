"""The confirmation state machine for one customer's conversation (D26, D53-D55).

The only way money moves:
  1. The model calls `propose(from_last4, to_last4, amount)`. Policy is checked in code;
     a pending transfer is recorded; the runtime speaks the code-written read-back and
     ends the turn. The model cannot confirm anything.
  2. The user's next message goes to `on_user_message` BEFORE the model sees it.
     A clear yes (confirm.py) executes the transfer; anything else cancels it and the
     message then goes to the model as usual. Either way the pending transfer is gone
     after that one message (expiry after one user turn).
  3. A pending transfer also expires after `max_age` (default 2 minutes). The same
     limit is enforced again inside nessie-tools (D54).

At most one transfer can wait for an answer. A second `propose` while one is pending
can only happen if the model asked for two transfers in one turn; the user can't know
which read-back they heard, so BOTH are cancelled and the model asks again (R2-01).
Because of that, a refused proposal never leaves anything pending that a "yes" could
confirm (R2-02).

A temporary failure at execution (D39) keeps the transfer pending and repeats the
read-back, so a fresh yes is needed; this happens at most `max_retries` times and
never extends the original deadline. Any other error at execution is caught: the
transfer's real status is read back, and the user is told "nothing was moved" only if
that is true (R2-04, R2-05).

Every decision is written to the audit log's safety events (D55), with this session's
id and the transfer's real status afterwards (`outcome`, R2-08). Messages that weren't
a yes are not stored, only that they weren't a yes.

One SafetySession per conversation. Its methods are serialized by a lock, so a voice
runtime's idle timer can call `expire_if_stale` from another thread (R2-05).
"""

from __future__ import annotations

import contextlib
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from nessie_tools import (
    AccountType,
    AuditLog,
    BankBackend,
    BankError,
    TransferExpired,
    TransferRejected,
    TransferService,
    TransferStatus,
    compute_balance,
)

from ledgervoice.safety.confirm import is_clear_yes
from ledgervoice.safety.policy import (
    Proposal,
    ProposalRejected,
    check_card_payment,
    check_proposal,
)

DEFAULT_MAX_AGE = timedelta(minutes=2)
DEFAULT_MAX_RETRIES = 2
NOTHING_MOVED = frozenset({TransferStatus.CANCELLED.value, TransferStatus.EXPIRED.value})
UNKNOWN = "UNKNOWN"  # outcome when the transfer's status could not be read


class Event(StrEnum):
    """Kinds of safety events written to the audit log (D55, R2-08)."""

    PROPOSED = "proposed"
    PROPOSAL_REFUSED = "proposal_refused"
    CANCELLED_DOUBLE = "cancelled_second_proposal"  # a 2nd proposal arrived while pending
    NOT_A_YES = "cancelled_not_a_yes"
    EXPIRED = "expired"  # the session's own time limit
    EXPIRED_AT_EXECUTION = "expired_at_execution"  # caught by the nessie-tools backstop
    CONFIRMED = "confirmed"
    RETRY_OFFERED = "retry_offered"
    EXECUTION_REFUSED = "execution_refused"
    EXECUTION_ERROR = "execution_error"  # an unexpected error while executing
    EXECUTED = "executed"  # execution ran; `outcome` says how it ended
    STATE_MISMATCH = "state_mismatch"  # a cancel found the transfer not cancellable


@dataclass(frozen=True)
class ProposeResult:
    """`ok`: the runtime says `say` (the read-back) and ends the turn.
    Not ok: `say` goes back to the model as the tool result, to explain to the user."""

    ok: bool
    say: str
    transfer_id: str | None = None


@dataclass(frozen=True)
class Intercept:
    """What happened to a user message while a transfer was pending.

    handled=True: the message was the confirmation; the runtime says `reply` itself and
    the message is NOT passed to the model. handled=False: pass the message to the model
    as usual, with `note` (if any) added as context about what happened."""

    handled: bool
    reply: str | None = None
    note: str | None = None


PASS_THROUGH = Intercept(handled=False)


@dataclass
class _Pending:
    transfer_id: str
    proposal: Proposal
    created: datetime
    retries: int = 0


class SafetySession:
    def __init__(
        self,
        backend: BankBackend,
        service: TransferService,
        audit: AuditLog,
        customer_id: str,
        *,
        session_id: str | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_age: timedelta = DEFAULT_MAX_AGE,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._bank = backend
        self._service = service
        self._audit = audit
        self.customer_id = customer_id
        self.session_id = session_id or f"sess-{uuid.uuid4().hex[:12]}"
        self._now = now
        self._max_age = max_age
        self._max_retries = max_retries
        self._pending: _Pending | None = None
        self._lock = threading.RLock()

    @property
    def pending_transfer_id(self) -> str | None:
        with self._lock:
            return self._pending.transfer_id if self._pending else None

    # --- step 1: the model proposes ----------------------------------------------
    def propose(self, from_last4: object, to_last4: object, amount: object) -> ProposeResult:
        """The model's only tool. Arguments arrive from the model, so they are checked
        here: anything that is not text is refused, never converted (R2-11)."""
        with self._lock:
            if self._pending is not None:  # R2-01: two proposals in one turn
                return self._cancel_both(self._pending)
            if not (
                isinstance(from_last4, str)
                and isinstance(to_last4, str)
                and isinstance(amount, str)
            ):
                return self._refuse("the accounts and the amount must be given as text")
            try:
                accounts = self._bank.list_accounts(self.customer_id)
                proposal = check_proposal(accounts, from_last4, to_last4, amount)
                if proposal.destination.type is AccountType.CREDIT_CARD:  # R2-14
                    owed = -compute_balance(
                        self._bank, proposal.destination.id, account=proposal.destination
                    ).current
                    check_card_payment(proposal, owed)
            except ProposalRejected as exc:
                return self._refuse(str(exc))
            except BankError:
                return self._refuse("I couldn't look up your accounts just now; please try again")

            try:
                record = self._service.create_pending(
                    self.customer_id, proposal.source.id, proposal.destination.id, proposal.amount
                )
            except TransferRejected as exc:
                return self._refuse(str(exc))
            # R2-03: the read-back is only returned once the proposal is logged; if
            # anything fails before that, the transfer is cancelled and nothing waits.
            try:
                self._log(
                    Event.PROPOSED,
                    record.transfer_id,
                    _summary(proposal),
                    outcome=TransferStatus.PENDING.value,
                )
            except BaseException:
                with contextlib.suppress(Exception):
                    self._cancel(record.transfer_id, "could not log the proposal")
                raise
            self._pending = _Pending(record.transfer_id, proposal, self._now())
            return ProposeResult(True, proposal.read_back(), record.transfer_id)

    # --- step 2: the user answers ------------------------------------------------
    def on_user_message(self, text: str) -> Intercept:
        with self._lock:
            pending = self._pending
            if pending is None:
                return PASS_THROUGH
            self._pending = None  # whatever happens now, this message settles it
            tid = pending.transfer_id

            if self._now() - pending.created > self._max_age:
                status = self._cancel(tid, "no answer within the time limit", expired=True)
                self._log(Event.EXPIRED, tid, outcome=status)
                return Intercept(
                    handled=False,
                    note=f"The transfer request ({_summary(pending.proposal)}) expired before "
                    f"the user answered. {_moved_note(status)}",
                )

            if not isinstance(text, str) or not is_clear_yes(text):
                status = self._cancel(tid, "the user's reply was not a clear yes")
                self._log(Event.NOT_A_YES, tid, outcome=status)
                return Intercept(
                    handled=False,
                    note=f"The transfer request ({_summary(pending.proposal)}) was cancelled "
                    f"because the user did not say yes. {_moved_note(status)}",
                )

            self._log(Event.CONFIRMED, tid, outcome=TransferStatus.PENDING.value)
            return self._execute(pending)

    def expire_if_stale(self) -> str | None:
        """For runtimes with idle timers (voice): expire a pending transfer that has
        waited too long. Returns a note for the model, or None if nothing expired."""
        with self._lock:
            pending = self._pending
            if pending is None or self._now() - pending.created <= self._max_age:
                return None
            self._pending = None
            status = self._cancel(pending.transfer_id, "no answer within the time limit", True)
            self._log(Event.EXPIRED, pending.transfer_id, outcome=status)
            return (
                f"The transfer request ({_summary(pending.proposal)}) expired. "
                f"{_moved_note(status)}"
            )

    # --- execution -----------------------------------------------------------------
    def _execute(self, pending: _Pending) -> Intercept:
        tid = pending.transfer_id
        try:
            result = self._service.execute_pending(tid, customer_id=self.customer_id)
        except TransferExpired as exc:  # the nessie-tools backstop (D54)
            status = self._status(tid)
            self._log(Event.EXPIRED_AT_EXECUTION, tid, str(exc), outcome=status)
            return Intercept(
                handled=True,
                reply=f"That request had expired, so I didn't make the transfer. "
                f"{_moved_reply(status)}",
                note=f"The transfer was not made: it had expired. {_moved_note(status)}",
            )
        except TransferRejected as exc:
            if exc.retryable and pending.retries < self._max_retries:
                pending.retries += 1
                self._pending = pending  # still pending; a fresh yes is needed
                self._log(Event.RETRY_OFFERED, tid, str(exc), outcome=self._status(tid))
                return Intercept(
                    handled=True,
                    reply="I couldn't complete that just now. " + pending.proposal.read_back(),
                )
            if exc.retryable:  # out of retries: it is still PENDING, so cancel it
                status = self._cancel(tid, f"gave up after {pending.retries} retries: {exc}")
            else:  # nessie-tools already cancelled it; read what it says now
                status = self._status(tid)
            self._log(Event.EXECUTION_REFUSED, tid, str(exc), outcome=status)
            return Intercept(
                handled=True,
                reply=f"I couldn't make that transfer: {exc}. {_moved_reply(status)}",
                note=f"The transfer was not made. {_moved_note(status)}",
            )
        except Exception as exc:  # R2-04: anything else must not strand the transfer
            status = self._status(tid)
            if status == TransferStatus.PENDING.value:
                status = self._cancel(tid, f"error during execution: {type(exc).__name__}")
            self._log(Event.EXECUTION_ERROR, tid, f"{type(exc).__name__}: {exc}", outcome=status)
            if status in NOTHING_MOVED:
                reply = "Something went wrong, so I didn't make that transfer. Nothing was moved."
            else:
                reply = (
                    "Something went wrong and I can't confirm whether that transfer went "
                    "through. Please check your balances before trying again."
                )
            return Intercept(handled=True, reply=reply, note=f"Transfer status: {status}.")
        self._log(Event.EXECUTED, tid, result.message, outcome=result.status.value)
        return Intercept(handled=True, reply=result.message, note=result.message)

    # --- helpers -----------------------------------------------------------------
    def _cancel_both(self, pending: _Pending) -> ProposeResult:
        self._pending = None
        status = self._cancel(
            pending.transfer_id, "a second transfer was proposed before an answer"
        )
        self._log(Event.CANCELLED_DOUBLE, pending.transfer_id, outcome=status)
        self._log(Event.PROPOSAL_REFUSED, None, "proposed while another transfer was pending")
        moved = (
            "nothing was moved"
            if status in NOTHING_MOVED
            else f"the earlier one's status is {status}, so check the balances"
        )
        return ProposeResult(
            False,
            "Two transfers were requested at once, so neither was set up and the earlier "
            f"one was cancelled ({moved}). Ask the user which single transfer they want.",
        )

    def _refuse(self, reason: str) -> ProposeResult:
        self._log(Event.PROPOSAL_REFUSED, None, reason)
        end = "" if reason.endswith((".", "?", "!")) else "."
        return ProposeResult(False, f"That transfer can't be set up: {reason}{end}")

    def _cancel(self, transfer_id: str, reason: str, expired: bool = False) -> str:
        """Cancel if still pending, and return the transfer's real status afterwards
        (R2-05). A status other than CANCELLED/EXPIRED means something else acted on
        it (another thread or runtime); that is logged as a mismatch."""
        try:
            record = self._service.cancel_pending(
                transfer_id, customer_id=self.customer_id, expired=expired, reason=reason
            )
            status = record.status.value
        except Exception:  # not pending any more, or the audit log failed: look
            status = self._status(transfer_id)
        if status not in NOTHING_MOVED:
            self._log(Event.STATE_MISMATCH, transfer_id, f"cancel: {reason}", outcome=status)
        return status

    def _status(self, transfer_id: str) -> str:
        try:
            record = self._audit.find(transfer_id)
        except Exception:
            return UNKNOWN
        return record.status.value if record else UNKNOWN

    def _log(
        self, kind: Event, transfer_id: str | None, detail: str = "", *, outcome: str = ""
    ) -> None:
        self._audit.log_event(
            self.customer_id,
            kind.value,
            transfer_id=transfer_id,
            detail=detail[:300],
            session_id=self.session_id,
            outcome=outcome,
        )


def _summary(p: Proposal) -> str:
    return f"${p.amount:,} from ...{p.source.last4} to ...{p.destination.last4}"


def _moved_note(status: str) -> str:
    if status in NOTHING_MOVED:
        return "Nothing was moved."
    return f"Its status is {status}, so do not tell the user nothing was moved."


def _moved_reply(status: str) -> str:
    if status in NOTHING_MOVED:
        return "Nothing was moved."
    return "I can't confirm what happened, so please check your balances."
