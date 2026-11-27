"""What a conversation turn produces, as a stream of events (D58).

The engine never prints or speaks; it yields these. The terminal client (`make chat`)
prints them; the M3 web frontend will send the same events over a WebSocket and
render them, for typed and spoken input alike.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Source = Literal["typed", "spoken"]


@dataclass(frozen=True)
class TextDelta:
    """A piece of the model's reply, as it streams."""

    text: str


@dataclass(frozen=True)
class ToolStarted:
    """The agent is looking something up (a UI can show "checking your accounts...")."""

    name: str


@dataclass(frozen=True)
class ToolFinished:
    """Not sent for a successful propose_transfer: its ReadBack is the finish, and
    nothing is yielded between the proposal and the ReadBack (so a client can't stop
    reading in between and leave an unseen transfer pending)."""

    name: str
    ok: bool


@dataclass(frozen=True)
class ReadBack:
    """The exact transfer confirmation question, written by code (D26). Shown or
    spoken word for word; the user's next message is the answer. A UI may render it as
    a confirmation card, but it must show this text."""

    text: str
    transfer_id: str


@dataclass(frozen=True)
class RuntimeReply:
    """A reply written by the app, not the model: the outcome after a "yes", or a
    fixed message when something went wrong."""

    text: str


@dataclass(frozen=True)
class TurnEnded:
    """`reason`: "model" (the model finished), "read_back", "confirmation" (a pending
    transfer was settled without the model), "call_limit" or "error".
    `first_output_ms`: from the user's message to the first text shown (TTFT)."""

    reason: str
    model_calls: int
    first_output_ms: float | None


Event = TextDelta | ToolStarted | ToolFinished | ReadBack | RuntimeReply | TurnEnded
