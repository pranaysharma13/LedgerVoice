"""The conversation engine (D58): one customer's conversation, independent of any UI.

    conversation = Conversation(model, tools, safety, system)
    for event in conversation.send("move $50 to savings", source="typed"):
        ...  # render TextDelta / ToolStarted / ReadBack / RuntimeReply / TurnEnded

A turn:
  1. The user's message goes to the SafetySession FIRST, before the model sees it
     (D26), for every message, even when nothing is pending (V3-03).
     - If it settled a pending transfer (a clear yes), the app's own reply is the whole
       turn; the model is not called.
     - Otherwise the message goes to the model, with the session's note (if any) in
       front of it as an "[App note: ...]".
  2. The model streams; it may call one tool per response (parallel calls are off).
     Tool results go back to it, up to MAX_MODEL_CALLS model calls per turn.
  3. A successful propose_transfer ends the turn at once: the code-written read-back
     is shown and stored in the history word for word, so the model sees exactly what
     the user saw. The user's next message is the answer (step 1).

All state lives on the instance (history, safety session, tools), so many
conversations can run side by side, one per visitor (D58, M7). The history always
keeps a valid user/assistant alternation, even if the caller stops consuming events
part-way (e.g. a barge-in later).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Any

from ledgervoice.agent.events import (
    Event,
    ReadBack,
    RuntimeReply,
    Source,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnEnded,
)
from ledgervoice.agent.model import ModelClient, ModelResponse, TextChunk
from ledgervoice.agent.tools import TOOL_SCHEMAS, BankTools
from ledgervoice.safety import SafetySession

MAX_MODEL_CALLS = 5
SORRY = "Sorry, something went wrong on my side. Could you try that again?"
CALL_LIMIT = "Sorry, I couldn't work that out. Could you ask it a simpler way?"
INTERRUPTED = "(interrupted)"
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnRecord:
    """What happened in one turn, without the message text."""

    source: Source
    reason: str
    model_calls: int
    first_output_ms: float | None


class Conversation:
    def __init__(
        self,
        model: ModelClient,
        tools: BankTools,
        safety: SafetySession,
        system: str,
        *,
        max_model_calls: int = MAX_MODEL_CALLS,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._model = model
        self._tools = tools
        self._safety = safety
        self._system = system
        self._max_calls = max_model_calls
        self._clock = clock
        self._history: list[dict[str, Any]] = []
        self.turns: list[TurnRecord] = []

    @property
    def session_id(self) -> str:
        return self._safety.session_id

    @property
    def history(self) -> list[dict[str, Any]]:
        """A copy of what the model sees (for tests and debugging)."""
        return [dict(m) for m in self._history]

    def send(self, text: str, source: Source = "typed") -> Generator[Event, None, None]:
        started = self._clock()
        first: list[float] = []
        calls = 0
        reason = "error"

        def shown() -> None:
            if not first:
                first.append(round((self._clock() - started) * 1000, 1))

        def ended(why: str) -> TurnEnded:
            nonlocal reason
            reason = why
            return TurnEnded(why, calls, first[0] if first else None)

        try:
            # 1. the safety session sees the message first, always (D26, V3-03)
            intercept = self._safety.on_user_message(text)
            if intercept.handled:
                reply = intercept.reply or SORRY
                self._history.append(_user([_text(text)]))
                self._history.append(_assistant([_text(reply)]))
                shown()
                yield RuntimeReply(reply)
                yield ended("confirmation")
                return
            blocks = [_text(text)]
            if intercept.note:
                blocks.insert(0, _text(f"[App note: {intercept.note}]"))
            self._history.append(_user(blocks))

            # 2. the model, with tools
            while calls < self._max_calls:
                calls += 1
                response: ModelResponse | None = None
                try:
                    for item in self._model.stream(
                        system=self._system, messages=self._history, tools=TOOL_SCHEMAS
                    ):
                        if isinstance(item, TextChunk):
                            shown()
                            yield TextDelta(item.text)
                        else:
                            response = item
                    if response is None:
                        raise RuntimeError("the model stream ended without a response")
                except Exception:
                    log.exception("session %s: model call failed", self.session_id)
                    self._history.append(_assistant([_text(SORRY)]))
                    shown()
                    yield RuntimeReply(SORRY)
                    yield ended("error")
                    return

                content = response.content or [_text("(no reply)")]
                self._history.append(_assistant(content))
                tool_uses = [b for b in content if b.get("type") == "tool_use"]
                if not tool_uses:
                    yield ended("model")
                    return

                results: list[dict[str, Any]] = []
                read_back: tuple[str, str] | None = None
                for use in tool_uses:
                    if read_back is not None:  # nothing runs after a proposal
                        results.append(_tool_error(use["id"], "not run: a transfer is waiting"))
                        continue
                    yield ToolStarted(use["name"])
                    outcome = self._tools.run(use["name"], use.get("input"))
                    result: dict[str, Any] = {
                        "type": "tool_result",
                        "tool_use_id": use["id"],
                        "content": outcome.content,
                    }
                    if outcome.is_error:
                        result["is_error"] = True
                    results.append(result)
                    if outcome.read_back and outcome.transfer_id:
                        # No yield until the ReadBack event: a caller that stops reading
                        # in between must not leave a transfer pending that the user
                        # never saw (a later "yes" would confirm it).
                        read_back = (outcome.read_back, outcome.transfer_id)
                        continue
                    yield ToolFinished(use["name"], not outcome.is_error)

                # 3. a proposal ends the turn with the code-written read-back (D26)
                if read_back is not None:
                    self._history.append(_user(results))
                    self._history.append(_assistant([_text(read_back[0])]))
                    shown()
                    reason = "read_back"
                    yield ReadBack(read_back[0], read_back[1])
                    yield ended("read_back")
                    return
                self._history.append(_user(results))

            self._history.append(_assistant([_text(CALL_LIMIT)]))
            shown()
            yield RuntimeReply(CALL_LIMIT)
            yield ended("call_limit")
        finally:
            self._repair()
            self.turns.append(TurnRecord(source, reason, calls, first[0] if first else None))

    def _repair(self) -> None:
        """Keep the history valid if the caller stopped consuming events part-way:
        every tool_use gets a result and the history ends with the assistant."""
        if not self._history:
            return
        last = self._history[-1]
        if last["role"] == "assistant":
            pending = [b for b in last["content"] if b.get("type") == "tool_use"]
            if not pending:
                return
            self._history.append(
                _user(
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": b["id"],
                            "content": "interrupted before this ran",
                            "is_error": True,
                        }
                        for b in pending
                    ]
                )
            )
        self._history.append(_assistant([_text(INTERRUPTED)]))


def _tool_error(tool_use_id: str, message: str) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": message, "is_error": True}


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _user(content: list[dict[str, Any]]) -> dict[str, Any]:
    return {"role": "user", "content": content}


def _assistant(content: list[dict[str, Any]]) -> dict[str, Any]:
    return {"role": "assistant", "content": content}
