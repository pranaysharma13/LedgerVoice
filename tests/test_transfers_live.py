"""End-to-end transfer against real Nessie: saga + computed balances + reconciliation.

Runs only with `make test-live`. Creates its own customer and accounts and deletes
the accounts afterwards.
"""

from __future__ import annotations

import pytest
from nessie_tools import (
    AccountType,
    Address,
    AuditLog,
    NessieBackend,
    NessieClient,
    TransferService,
    TransferStatus,
    compute_balance,
    reconcile,
)

pytestmark = pytest.mark.live

ADDRESS = Address(
    street_number="1", street_name="Saga St", city="Arlington", state="VA", zip="22201"
)


def test_live_transfer_moves_money_and_reconciles(tmp_path):
    from ledgervoice.config import get_settings

    s = get_settings()
    s.require("nessie_api_key")
    assert s.nessie_api_key is not None
    with (
        NessieClient(s.nessie_api_key.get_secret_value(), base_url=s.nessie_base_url) as client,
        AuditLog(tmp_path / "audit.sqlite") as audit,
    ):
        bank = NessieBackend(client)
        customer = bank.create_customer("Sam", "Saga", ADDRESS)
        checking = bank.create_account(customer.id, AccountType.CHECKING, "Saga Checking", 300)
        savings = bank.create_account(customer.id, AccountType.SAVINGS, "Saga Savings", 100)
        try:
            service = TransferService(bank, audit)
            pending = service.create_pending(customer.id, checking.id, savings.id, 25)
            result = service.execute_pending(pending.transfer_id, customer_id=customer.id)
            assert result.status is TransferStatus.COMPLETED, result.message
            assert compute_balance(bank, checking.id).current == 275
            assert compute_balance(bank, savings.id).current == 125

            report = reconcile(bank, audit, [checking.id, savings.id], customer_ids=[customer.id])
            assert report.ok, report.violations
            assert report.legs_found == 2
        finally:
            bank.delete_account(checking.id)
            bank.delete_account(savings.id)
