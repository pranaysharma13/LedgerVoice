"""nessie-tools: a typed, testable layer over the Capital One Nessie sandbox API.

Layers (bottom to top):
- NessieClient:  HTTP + key redaction + HTTPS enforcement
- BankBackend:   plain storage operations, implemented by
                 NessieBackend (live API) and FakeBackend (in-memory, for tests)
- Domain, written once on top of any backend:
    balances   computed balances (D18)
    audit      SQLite audit log for transfers (D22)
    transfers  transfer saga with compensation (D15)
    reconcile  audit log vs bank records
"""

from nessie_tools.audit import (
    AuditError,
    AuditLog,
    TransferEvent,
    TransferRecord,
    TransferStatus,
)
from nessie_tools.backend import BankBackend
from nessie_tools.balances import Balance, compute_balance
from nessie_tools.client import DEFAULT_BASE_URL, NessieClient, redact_key
from nessie_tools.errors import (
    AmountError,
    BackendFailure,
    BankError,
    LedgerError,
    NessieDataError,
    NessieError,
    NotFoundError,
    is_ambiguous,
    is_temporary,
)
from nessie_tools.fake_backend import FakeBackend
from nessie_tools.models import (
    Account,
    AccountType,
    Address,
    Bill,
    Customer,
    Deposit,
    Merchant,
    Purchase,
    TxStatus,
    Withdrawal,
)
from nessie_tools.money import require_whole_dollars
from nessie_tools.nessie_backend import NessieBackend
from nessie_tools.reconcile import ReconcileReport, Violation, ViolationKind, reconcile
from nessie_tools.transfers import (
    TransferRejected,
    TransferResult,
    TransferService,
    new_transfer_id,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "Account",
    "AccountType",
    "Address",
    "AmountError",
    "AuditError",
    "AuditLog",
    "BackendFailure",
    "Balance",
    "BankBackend",
    "BankError",
    "Bill",
    "Customer",
    "Deposit",
    "FakeBackend",
    "LedgerError",
    "Merchant",
    "NessieBackend",
    "NessieClient",
    "NessieDataError",
    "NessieError",
    "NotFoundError",
    "Purchase",
    "ReconcileReport",
    "TransferEvent",
    "TransferRecord",
    "TransferRejected",
    "TransferResult",
    "TransferService",
    "TransferStatus",
    "TxStatus",
    "Violation",
    "ViolationKind",
    "Withdrawal",
    "compute_balance",
    "is_ambiguous",
    "is_temporary",
    "new_transfer_id",
    "reconcile",
    "redact_key",
    "require_whole_dollars",
]
