"""The conversation safety layer (M1d): money moves only after a clear, recent yes.

See session.py for the state machine, policy.py for what may be proposed, confirm.py
for what counts as a yes and amounts.py for amount parsing.
"""

from ledgervoice.safety.amounts import MAX_TRANSFER, AmountRejected, parse_amount
from ledgervoice.safety.confirm import YES_PHRASES, is_clear_yes
from ledgervoice.safety.policy import Proposal, ProposalRejected, check_proposal
from ledgervoice.safety.session import (
    Event,
    Intercept,
    ProposeResult,
    SafetySession,
)

__all__ = [
    "MAX_TRANSFER",
    "YES_PHRASES",
    "AmountRejected",
    "Event",
    "Intercept",
    "ProposalRejected",
    "Proposal",
    "ProposeResult",
    "SafetySession",
    "check_proposal",
    "is_clear_yes",
    "parse_amount",
]
