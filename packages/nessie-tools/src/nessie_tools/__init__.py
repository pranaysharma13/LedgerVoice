"""nessie-tools: a typed, testable layer over the Capital One Nessie sandbox API.

Layers (bottom to top):
- NessieClient:  HTTP + key redaction + HTTPS enforcement
- BankBackend:   plain storage operations, implemented by
                 NessieBackend (live API) and FakeBackend (in-memory, for tests)
- (M1b) balances, transfers, audit log, reconciliation: written once on top
"""

from nessie_tools.backend import BankBackend
from nessie_tools.client import DEFAULT_BASE_URL, NessieClient, redact_key
from nessie_tools.errors import (
    AmountError,
    BackendFailure,
    BankError,
    NessieError,
    NotFoundError,
    is_ambiguous,
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

__all__ = [
    "DEFAULT_BASE_URL",
    "Account",
    "AccountType",
    "Address",
    "AmountError",
    "BackendFailure",
    "BankBackend",
    "BankError",
    "Bill",
    "Customer",
    "Deposit",
    "FakeBackend",
    "Merchant",
    "NessieBackend",
    "NessieClient",
    "NessieError",
    "NotFoundError",
    "Purchase",
    "TxStatus",
    "Withdrawal",
    "is_ambiguous",
    "redact_key",
    "require_whole_dollars",
]
