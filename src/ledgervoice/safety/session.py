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

A temporary failure at execution (D39) keeps the transfer pending and repeats the
read-back, so a fresh yes is needed; this happens at most `max_retries` times and
never extends the original deadline. Every decision is written to the audit log's
safety events (D55), including proposals that were refused and messages that weren't
a yes (their text is not stored, only that they weren't a yes).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from nessie_tools import (
    AuditLog,
    BankBackend,
    BankError,
    LedgerError,
    TransferRejected,
    TransferService,
)

from ledgervoice.safety.confirm import is_clear_yes
from ledgervoice.safety.policy import Proposal, ProposalRejected, check_proposal

DEFAULT_MAX_AGE = timedelta(minutes=2)
DEFAULT_MAX_RETRIES = 2


class Event(StrEnum):
    """Kinds of safety events written to the audit log (D55)."""

    PROPOSED = "proposed"
    PROPOSAL_REFUSED = "proposal_refused"
    REPLACED = "replaced"
    NOT_A_YES = "cancelled_not_a_yes"
    EXPIRED = "expired"
    CONFIRMED = "confirmed"
    RETRY_OFFERED = "retry_offered"
    EXECUTION_REFUSED = "execution_refused"
    EXECUTED = "executed"


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
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_age: timedelta = DEFAULT_MAX_AGE,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._bank = backend
        self._service = service
        self._audit = audit
        self.customer_id = customer_id
        self._now = now
        self._max_age = max_age
        self._max_retries = max_retries
        self._pending: _Pending | None = None

    @property
    def pending_transfer_id(self) -> str | None:
        return self._pending.transfer_id if self._pending else None

    # --- step 1: the model proposes ----------------------------------------------
    def propose(self, from_last4: str, to_last4: str, amount: str) -> ProposeResult:
        try:
            accounts = self._bank.list_accounts(self.customer_id)
            proposal = check_proposal(accounts, from_last4, to_last4, amount)
        except ProposalRejected as exc:
            return self._refuse(str(exc))
        except BankError:
            return self._refuse("I couldn't look up your accounts just now; please try again")

        if self._pending is not None:  # one pending transfer per conversation (D53)
            self._cancel(self._pending, "replaced by a new request")
            self._log(Event.REPLACED, self._pending.transfer_id)
            self._pending = None

        try:
            record = self._service.create_pending(
                self.customer_id, proposal.source.id, proposal.destination.id, proposal.amount
            )
        except TransferRejected as exc:
            return self._refuse(str(exc))
        self._pending = _Pending(record.transfer_id, proposal, self._now())
        self._log(Event.PROPOSED, record.transfer_id, _summary(proposal))
        return ProposeResult(True, proposal.read_back(), record.transfer_id)

    # --- step 2: the user answers ------------------------------------------------
    def on_user_message(self, text: str) -> Intercept:
        pending = self._pending
        if pending is None:
            return PASS_THROUGH
        self._pending = None  # whatever happens now, this message settles it

        if self._now() - pending.created > self._max_age:
            self._cancel(pending, "no answer within the time limit", expired=True)
            self._log(Event.EXPIRED, pending.transfer_id)
            return Intercept(
                handled=False,
                note=f"The transfer request ({_summary(pending.proposal)}) expired before "
                "the user answered. Nothing was moved.",
            )

        if not is_clear_yes(text):
            self._cancel(pending, "the user's reply was not a clear yes")
            self._log(Event.NOT_A_YES, pending.transfer_id)
            return Intercept(
                handled=False,
                note=f"The transfer request ({_summary(pending.proposal)}) was cancelled "
                "because the user did not say yes. Nothing was moved.",
            )

        self._log(Event.CONFIRMED, pending.transfer_id)
        try:
            result = self._service.execute_pending(
                pending.transfer_id, customer_id=self.customer_id
            )
        except TransferRejected as exc:
            if exc.retryable and pending.retries < self._max_retries:
                pending.retries += 1
                self._pending = pending  # still pending; a fresh yes is needed
                self._log(Event.RETRY_OFFERED, pending.transfer_id, str(exc))
                return Intercept(
                    handled=True,
                    reply="I couldn't complete that just now. " + pending.proposal.read_back(),
                )
            if exc.retryable:  # out of retries: it is still PENDING, so cancel it
                self._cancel(pending, f"gave up after {pending.retries} retries: {exc}")
            self._log(Event.EXECUTION_REFUSED, pending.transfer_id, str(exc))
            return Intercept(
                handled=True,
                reply=f"I couldn't make that transfer: {exc}. Nothing was moved.",
                note="The transfer was not made.",
            )
        self._log(Event.EXECUTED, pending.transfer_id, result.status.value)
        return Intercept(handled=True, reply=result.message, note=result.message)

    def expire_if_stale(self) -> str | None:
        """For runtimes with idle timers (voice): expire a pending transfer that has
        waited too long. Returns a note for the model, or None if nothing expired."""
        pending = self._pending
        if pending is None or self._now() - pending.created <= self._max_age:
            return None
        self._pending = None
        self._cancel(pending, "no answer within the time limit", expired=True)
        self._log(Event.EXPIRED, pending.transfer_id)
        return f"The transfer request ({_summary(pending.proposal)}) expired. Nothing was moved."

    # --- helpers -----------------------------------------------------------------
    def _refuse(self, reason: str) -> ProposeResult:
        self._log(Event.PROPOSAL_REFUSED, None, reason)
        return ProposeResult(False, f"That transfer can't be set up: {reason}.")

    def _cancel(self, pending: _Pending, reason: str, *, expired: bool = False) -> None:
        """Cancel if still pending. If it isn't (e.g. nessie-tools already expired it),
        there is nothing left to cancel and that is fine."""
        try:
            self._service.cancel_pending(
                pending.transfer_id, customer_id=self.customer_id, expired=expired, reason=reason
            )
        except (TransferRejected, LedgerError):
            pass

    def _log(self, kind: Event, transfer_id: str | None, detail: str = "") -> None:
        self._audit.log_event(
            self.customer_id, kind.value, transfer_id=transfer_id, detail=detail[:300]
        )


def _summary(p: Proposal) -> str:
    return f"${p.amount:,} from ...{p.source.last4} to ...{p.destination.last4}"
