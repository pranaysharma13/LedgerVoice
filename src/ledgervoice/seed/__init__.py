"""Seed data for LedgerVoice (M1c): a deterministic plan plus its ground truth.

M1c-1 (this part) is offline: generate a plan and compute the answer key. M1c-2 adds
the applier that writes a plan to a bank, the manifest, read-back verification and reset.
"""

from ledgervoice.seed.generator import SeedConfigError, generate
from ledgervoice.seed.plan import (
    PlanAccount,
    PlanBill,
    PlanCustomer,
    PlanMerchant,
    PlanTx,
    SeedPlan,
    TxKind,
)
from ledgervoice.seed.truth import GroundTruth, Spend

__all__ = [
    "GroundTruth",
    "PlanAccount",
    "PlanBill",
    "PlanCustomer",
    "PlanMerchant",
    "PlanTx",
    "SeedConfigError",
    "SeedPlan",
    "Spend",
    "TxKind",
    "generate",
]
