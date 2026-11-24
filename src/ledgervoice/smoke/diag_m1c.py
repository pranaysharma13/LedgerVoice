"""One-off M1c diagnostic: three Nessie facts the seeder depends on.

Q1 (V1-08) Can an account be created for a customer id that doesn't exist?
Q2 (V1-08) Can a purchase be created with a merchant id that doesn't exist?
Q3          Which bill `status` values does Nessie accept, and what does it store?

What it writes to the sandbox: one customer ("Lee Diag"), one checking account, at most
one account under a made-up customer id (only if Q1 is "accepted"), at most one purchase
and a few bills. It deletes every account it created at the end. As with earlier probes,
the customer and the transactions stay behind (Nessie has no referential integrity, D36).
It never calls DELETE /data.

Run from the repo root:  .venv/bin/python -m ledgervoice.smoke.diag_m1c
"""

from __future__ import annotations

import sys
import uuid
from dataclasses import dataclass
from datetime import date

from nessie_tools import (
    AccountType,
    Address,
    BankBackend,
    BankError,
    BillStatus,
    NessieBackend,
    NessieClient,
    NessieError,
    NotFoundError,
)

ADDRESS = Address(
    street_number="1", street_name="Diag Street", city="Testville", state="VA", zip="22101"
)
# An invalid status, sent raw (bypassing BillStatus, which now rejects it before any
# request) to check that Nessie itself validates the field. Result 2026-11-23: it does.
INVALID_BILL_STATUS = "lv-not-a-status"


@dataclass(frozen=True)
class Finding:
    question: str
    answer: str  # ACCEPTED / REJECTED / ERROR / INFO
    detail: str = ""


def _error_detail(exc: BankError) -> str:
    """Status and (already key-redacted) body of a failed call, kept short."""
    if isinstance(exc, NessieError):
        body = (exc.body or "").replace("\n", " ")[:300]
        return f"HTTP {exc.status_code}: {body}" if exc.status_code else str(exc)[:300]
    return str(exc)[:300]


def run(backend: BankBackend, today: date, client: NessieClient | None = None) -> list[Finding]:
    """Ask the three questions. `client` (live runs only) sends the invalid bill status
    raw; without it that one probe is skipped."""
    findings: list[Finding] = []
    created_accounts: list[str] = []
    try:
        # Q1 -- account under a customer id that was never created
        ghost_customer = str(uuid.uuid4())
        q1 = "Q1 account for an unknown customer id"
        try:
            ghost = backend.create_account(ghost_customer, AccountType.CHECKING, "LV Diag Ghost", 0)
            created_accounts.append(ghost.id)
            findings.append(Finding(q1, "ACCEPTED", f"account {ghost.id} was created"))
        except NotFoundError as exc:
            findings.append(Finding(q1, "REJECTED", f"not found: {exc}"[:300]))
        except BankError as exc:
            findings.append(Finding(q1, "ERROR", _error_detail(exc)))

        # A real customer + account for Q2 and Q3
        customer = backend.create_customer("Lee", "Diag", ADDRESS)
        account = backend.create_account(customer.id, AccountType.CHECKING, "LV Diag Checking", 100)
        created_accounts.append(account.id)
        findings.append(Finding("setup", "INFO", f"customer {customer.id}, account {account.id}"))

        # Q2 -- purchase with a merchant id that was never created
        q2 = "Q2 purchase with an unknown merchant id"
        ghost_merchant = str(uuid.uuid4())
        try:
            purchase = backend.create_purchase(
                account.id, ghost_merchant, 5, today, "LV diag unknown merchant"
            )
            stored = [p for p in backend.list_purchases(account.id) if p.id == purchase.id]
            findings.append(
                Finding(
                    q2,
                    "ACCEPTED",
                    f"purchase {purchase.id} created; listed back: {'yes' if stored else 'NO'}",
                )
            )
        except NotFoundError as exc:
            findings.append(Finding(q2, "REJECTED", f"not found: {exc}"[:300]))
        except BankError as exc:
            findings.append(Finding(q2, "ERROR", _error_detail(exc)))

        # Q3 -- which bill statuses are accepted, and what is stored
        for status in BillStatus:
            q3 = f"Q3 bill status {status.value!r}"
            try:
                bill = backend.create_bill(
                    account.id, "LV Diag Power", f"diag-{status}", 10, today, 25, status=status
                )
                stored_status = next(
                    (b.status for b in backend.list_bills(account.id) if b.id == bill.id), None
                )
                stored_value = stored_status.value if stored_status else None
                findings.append(Finding(q3, "ACCEPTED", f"stored status: {stored_value!r}"))
            except BankError as exc:
                findings.append(Finding(q3, "REJECTED", _error_detail(exc)))
        q3 = f"Q3 bill status {INVALID_BILL_STATUS!r}"
        if client is None:
            findings.append(Finding(q3, "INFO", "skipped (needs the raw live client)"))
        else:
            payload = {
                "status": INVALID_BILL_STATUS,
                "payee": "LV Diag Power",
                "nickname": "diag-invalid",
                "payment_date": today.isoformat(),
                "payment_amount": 10,
            }
            try:
                client.request("POST", f"/accounts/{account.id}/bills", json=payload)
                findings.append(Finding(q3, "ACCEPTED", "Nessie stored an invalid status"))
            except BankError as exc:
                findings.append(Finding(q3, "REJECTED", _error_detail(exc)))
    finally:
        for account_id in created_accounts:
            try:
                backend.delete_account(account_id)
            except BankError as exc:
                findings.append(
                    Finding("cleanup", "ERROR", f"could not delete {account_id}: {exc}"[:300])
                )
    return findings


def main() -> int:
    from ledgervoice.config import get_settings  # same route as the live contract tests

    settings = get_settings()
    settings.require("nessie_api_key")
    assert settings.nessie_api_key is not None
    with NessieClient(
        settings.nessie_api_key.get_secret_value(), base_url=settings.nessie_base_url
    ) as client:
        findings = run(NessieBackend(client), date.today(), client)
    for f in findings:
        print(f"{f.question:<42} {f.answer:<9} {f.detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
