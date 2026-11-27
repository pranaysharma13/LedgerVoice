"""The streaming model call (D27, D59, D63), behind a small interface so tests can
script the model and the engine never touches the SDK directly.

Request settings, fixed here:
  - tool_choice auto with disable_parallel_tool_use: at most one tool per response
    (R2-01: the safety session also refuses double proposals, this is the first guard);
  - top-level cache_control (automatic prompt caching: the breakpoint moves forward
    with the conversation; Haiku 5.5 caches prompts of 512+ tokens);
  - output_config.effort (low for Haiku 5.5, D7).

Every call is written to the cost ledger as soon as it returns, before its content
is converted, and also when it fails (F-15), tagged with the session id.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from ledgervoice.costs import CostLedger

PRICE_TIER_LIMIT = 100_000  # Haiku 5.5 bills a request above this many input tokens higher


@dataclass(frozen=True)
class TextChunk:
    text: str


@dataclass(frozen=True)
class ModelResponse:
    """The finished response. `content` holds only text and tool_use blocks, as
    message-param dicts ready to append to the history."""

    content: list[dict[str, Any]]
    stop_reason: str | None
    usage: dict[str, int] = field(default_factory=dict)


StreamItem = TextChunk | ModelResponse


class ModelClient(Protocol):
    def stream(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Iterator[StreamItem]:
        """Yield TextChunks as text arrives, then exactly one ModelResponse."""
        ...


USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


class AnthropicModel:
    """ModelClient over the Anthropic SDK's `messages.stream`."""

    def __init__(
        self,
        client: Any,  # anthropic.Anthropic, or a test double with the same shape
        *,
        model: str,
        effort: str,
        ledger: CostLedger,
        session_id: str,
        max_tokens: int = 1024,
    ) -> None:
        self._client = client
        self.model = model
        self._effort = effort
        self._ledger = ledger
        self._session_id = session_id
        self._max_tokens = max_tokens

    def request(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """The exact request sent (a separate method so tests can check it)."""
        return {
            "model": self.model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": messages,
            "tools": tools,
            "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
            "cache_control": {"type": "ephemeral"},
            "output_config": {"effort": self._effort},
        }

    def stream(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Iterator[StreamItem]:
        kwargs = self.request(system=system, messages=messages, tools=tools)
        started = time.perf_counter()
        first_text: float | None = None
        try:
            with self._client.messages.stream(**kwargs) as stream:
                for event in stream:
                    if getattr(event, "type", None) == "text" and event.text:
                        if first_text is None:
                            first_text = time.perf_counter()
                        yield TextChunk(event.text)
                final = stream.get_final_message()
        except Exception as exc:
            self._record({}, started, first_text, stop_reason=None, error=type(exc).__name__)
            raise
        usage = {f: int(getattr(final.usage, f, 0) or 0) for f in USAGE_FIELDS}
        self._record(usage, started, first_text, stop_reason=final.stop_reason, error=None)
        yield ModelResponse(_blocks(final.content), final.stop_reason, usage)

    def _record(
        self,
        usage: dict[str, int],
        started: float,
        first_text: float | None,
        *,
        stop_reason: str | None,
        error: str | None,
    ) -> None:
        meta: dict[str, Any] = {
            "session_id": self._session_id,
            "model": self.model,
            "stop_reason": stop_reason,
            "ttft_ms": None if first_text is None else round((first_text - started) * 1000, 1),
        }
        if error is not None:
            meta["error"] = error
        prompt = sum(usage.get(f, 0) for f in USAGE_FIELDS if f != "output_tokens")
        if prompt > PRICE_TIER_LIMIT:
            meta["over_100k_price_tier"] = True  # the estimate below is then too low
        self._ledger.record(
            f"anthropic/{self.model}",
            "messages.stream",
            {k: float(v) for k, v in usage.items()},
            latency_ms=(time.perf_counter() - started) * 1000,
            **meta,
        )


def _blocks(content: list[Any]) -> list[dict[str, Any]]:
    """Keep only text and tool_use blocks, as plain dicts for the next request."""
    blocks: list[dict[str, Any]] = []
    for block in content:
        kind = getattr(block, "type", None)
        if kind == "text":
            if block.text:  # the API rejects empty text blocks in a later request
                blocks.append({"type": "text", "text": block.text})
        elif kind == "tool_use":
            blocks.append(
                {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
            )
    return blocks
