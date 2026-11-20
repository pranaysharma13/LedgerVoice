"""Transfers between a customer's own accounts, run as a saga (decision D15).

Nessie transfers have no destination field, so a transfer is two writes:
    leg 1: withdrawal from the source account
    leg 2: deposit into the destination account
Every leg's description starts with the transfer id (e.g. "LV-TRF-3f9a1c2e ..."),
which is how reconcile.py ties bank records back to the audit log.

A saga is a sequence of steps where each step that can fail after an earlier step
succeeded has a COMPENSATING action that undoes the earlier step:
- leg 1 fails          -> nothing moved: FAILED
- leg 2 fails          -> reversing deposit into the source: COMPENSATED
- reversal also fails  -> NEEDS_ATTENTION (a human must look; reconcile flags it)

Ambiguous outcomes: a timeout or 5xx can happen AFTER the bank wrote the leg. Nessie
has no idempotency keys, so blindly retrying could withdraw twice. Instead we look
the leg up by its exact description before deciding (see _verify_or_write).

This module does not decide whether a transfer is ALLOWED for the user (that is the
confirmation flow in M1d). It does re-check the basics as defense in depth.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from nessie_tools.audit import AuditLog, TransferRecord, TransferStatus
from nessie_tools.backend import BankBackend
from nessie_tools.balances import compute_balance
from nessie_tools.errors import BankError, is_ambiguous
from nessie_tools.models import Account, Deposit, Withdrawal
from nessie_tools.money import require_whole_dollars

S = TransferStatus

TRANSFER_ID_PREFIX = "LV-TRF-"
LEG1_MARKER = "(leg 1/2)"
LEG2_MARKER = "(leg 2/2)"
REVERSAL_MARKER = "(reversal of leg 1/2)"


def new_transfer_id() -> str:
    return f"{TRANSFER_ID_PREFIX}{secrets.token_hex(4)}"


def leg1_description(transfer_id: str, to_last4: str) -> str:
    return f"{transfer_id} transfer to acct ending {to_last4} {LEG1_MARKER}"


def leg2_description(transfer_id: str, from_last4: str) -> str:
    return f"{transfer_id} transfer from acct ending {from_last4} {LEG2_MARKER}"


def reversal_description(transfer_id: str) -> str:
    return f"{transfer_id} {REVERSAL_MARKER}"


class TransferRejected(BankError):
    """A pre-check failed. Nothing was written to the bank or the audit log."""


@dataclass(frozen=True)
class TransferResult:
    transfer_id: str
    status: TransferStatus
    amount: int
    from_last4: str
    to_last4: str
    message: str  # short, user-facing summary

    @property
    def succeeded(self) -> bool:
        return self.status is TransferStatus.COMPLETED


class TransferService:
    def __init__(
        self,
        backend: BankBackend,
        audit: AuditLog,
        today: Callable[[], date] = date.today,
        new_id: Callable[[], str] = new_transfer_id,
    ) -> None:
        self._bank = backend
        self._audit = audit
        self._today = today
        self._new_id = new_id

    # --- public ----------------------------------------------------------------
    def execute(
        self,
        customer_id: str,
        from_account_id: str,
        to_account_id: str,
        amount: int,
        transfer_id: str | None = None,
    ) -> TransferResult:
        """Run a transfer. Raises TransferRejected if a pre-check fails.

        If `transfer_id` names an existing PENDING record (created by the confirmation
        flow), that record is executed; it must match these accounts and amount.
        """
        amount = self._require_amount(amount)
        src, dst = self._check_accounts(customer_id, from_account_id, to_account_id)
        try:
            available = compute_balance(self._bank, src.id).current
        except BankError as exc:
            raise TransferRejected(f"could not read the balance: {exc}") from None
        if amount > available:
            raise TransferRejected(f"insufficient funds: ${available} available")

        record = self._begin(transfer_id, customer_id, src.id, dst.id, amount)
        tid = record.transfer_id
        when = self._today()

        # --- leg 1: withdrawal ----------------------------------------------------
        desc1 = leg1_description(tid, dst.last4)
        try:
            leg1 = self._verify_or_write(
                lambda: self._bank.create_withdrawal(src.id, amount, when, desc1),
                lambda: self._bank.list_withdrawals(src.id),
                desc1,
                amount,
            )
        except _Unverifiable as exc:
            return self._finish(tid, S.NEEDS_ATTENTION, f"leg 1 outcome unknown: {exc}", src, dst)
        if leg1 is None:
            return self._finish(tid, S.FAILED, "leg 1 (withdrawal) failed", src, dst)
        self._audit.transition(tid, S.LEG1_DONE, detail="withdrawal written", withdrawal_id=leg1.id)

        # --- leg 2: deposit -------------------------------------------------------
        desc2 = leg2_description(tid, src.last4)
        try:
            leg2 = self._verify_or_write(
                lambda: self._bank.create_deposit(dst.id, amount, when, desc2),
                lambda: self._bank.list_deposits(dst.id),
                desc2,
                amount,
            )
        except _Unverifiable as exc:
            return self._finish(tid, S.NEEDS_ATTENTION, f"leg 2 outcome unknown: {exc}", src, dst)
        if leg2 is not None:
            return self._finish(tid, S.COMPLETED, "deposit written", src, dst, deposit_id=leg2.id)

        # --- compensation: put the money back -------------------------------------
        desc_r = reversal_description(tid)
        try:
            reversal = self._verify_or_write(
                lambda: self._bank.create_deposit(src.id, amount, when, desc_r),
                lambda: self._bank.list_deposits(src.id),
                desc_r,
                amount,
            )
        except _Unverifiable as exc:
            reversal = None
            detail = f"leg 2 failed; reversal outcome unknown: {exc}"
        else:
            detail = "leg 2 failed; reversal failed"
        if reversal is None:
            return self._finish(tid, S.NEEDS_ATTENTION, detail, src, dst)
        return self._finish(
            tid, S.COMPENSATED, "leg 2 failed; reversal written", src, dst, reversal_id=reversal.id
        )

    # --- steps -----------------------------------------------------------------
    @staticmethod
    def _require_amount(amount: int) -> int:
        try:
            return require_whole_dollars(amount)
        except BankError as exc:
            raise TransferRejected(str(exc)) from None

    def _check_accounts(
        self, customer_id: str, from_account_id: str, to_account_id: str
    ) -> tuple[Account, Account]:
        if from_account_id == to_account_id:
            raise TransferRejected("source and destination are the same account")
        try:
            src = self._bank.get_account(from_account_id)
            dst = self._bank.get_account(to_account_id)
        except BankError as exc:
            raise TransferRejected(f"account lookup failed: {exc}") from None
        if src.customer_id != customer_id or dst.customer_id != customer_id:
            raise TransferRejected("both accounts must belong to the customer")
        return src, dst

    def _begin(
        self,
        transfer_id: str | None,
        customer_id: str,
        from_id: str,
        to_id: str,
        amount: int,
    ) -> TransferRecord:
        existing = self._audit.find(transfer_id) if transfer_id else None
        if existing is None:
            return self._audit.create(
                transfer_id or self._new_id(), customer_id, from_id, to_id, amount
            )
        if existing.status is not S.PENDING:
            raise TransferRejected(f"{existing.transfer_id} is {existing.status}, not PENDING")
        if (existing.customer_id, existing.from_account, existing.to_account, existing.amount) != (
            customer_id,
            from_id,
            to_id,
            amount,
        ):
            raise TransferRejected(f"{existing.transfer_id} does not match the pending transfer")
        return self._audit.transition(existing.transfer_id, S.EXECUTING, detail="confirmed")

    def _verify_or_write(
        self,
        write: Callable[[], Withdrawal | Deposit],
        list_existing: Callable[[], list[Withdrawal] | list[Deposit]],
        description: str,
        amount: int,
    ) -> Withdrawal | Deposit | None:
        """Write one leg. Returns the leg, or None if it definitely was NOT written.

        Raises _Unverifiable if the write was ambiguous and the check also failed.
        """
        try:
            return write()
        except BankError as exc:
            if not is_ambiguous(exc):
                return None  # the bank rejected it: nothing was written
            try:
                matches = [
                    leg
                    for leg in list_existing()
                    if leg.description == description and leg.amount == amount
                ]
            except BankError as check_exc:
                raise _Unverifiable(str(check_exc)) from None
            return matches[0] if matches else None

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
            tid, status, detail=detail, deposit_id=deposit_id, reversal_id=reversal_id
        )
        amount = record.amount
        messages = {
            S.COMPLETED: f"Moved ${amount} from ••{src.last4} to ••{dst.last4}.",
            S.FAILED: "The transfer didn't go through. No money moved.",
            S.COMPENSATED: (
                f"The transfer failed partway, so the ${amount} was returned to ••{src.last4}."
            ),
            S.NEEDS_ATTENTION: (
                f"The transfer hit an error and needs a manual check. Reference {tid}."
            ),
        }
        return TransferResult(tid, status, amount, src.last4, dst.last4, messages[status])


class _Unverifiable(Exception):
    """An ambiguous write whose outcome could not be checked either."""
