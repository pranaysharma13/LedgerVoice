"""Live: one short real conversation with Claude (D62). The bank is the in-memory
fake seeded with the real plan, so nothing is written to Nessie.

It proves the real API accepts our exact request (tools, parallel tool calls off,
automatic prompt caching, effort) and that the cache actually forms: by the second
model call, the ledger must show cache writes or reads (Haiku 5.5 caches prompts of
512+ tokens; our system prompt + tools are above that). Costs a fraction of a cent.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from nessie_tools import AuditLog

from ledgervoice.agent.chat import build_conversation
from ledgervoice.agent.events import TextDelta, TurnEnded
from ledgervoice.agent.model import AnthropicModel
from ledgervoice.costs import CostLedger
from ledgervoice.seed.apply import seed_fake
from ledgervoice.seed.generator import generate
from ledgervoice.seed.truth import GroundTruth

pytestmark = pytest.mark.live

ANCHOR = date(2026, 11, 24)


def test_a_real_balance_question_uses_the_tool_and_the_cache(tmp_path: Path) -> None:
    import anthropic

    from ledgervoice.config import get_settings

    settings = get_settings()
    settings.require("anthropic_api_key")
    assert settings.anthropic_api_key is not None
    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key.get_secret_value(), timeout=60.0
    )
    ledger_path = tmp_path / "costs.jsonl"
    ledger = CostLedger(ledger_path)
    plan = generate(42, ANCHOR)
    bank, manifest = seed_fake(plan)
    audit = AuditLog(":memory:")

    def make_model(session_id: str) -> AnthropicModel:
        return AnthropicModel(
            client,
            model=settings.claude_model,
            effort=settings.claude_effort,
            ledger=ledger,
            session_id=session_id,
        )

    conversation, _ = build_conversation(
        bank, audit, manifest.customers["priya"], ANCHOR, make_model
    )
    try:
        events = list(conversation.send("What's my checking balance?"))
    finally:
        client.close()
        audit.close()

    end = events[-1]
    assert isinstance(end, TurnEnded) and end.reason == "model", events
    reply = "".join(e.text for e in events if isinstance(e, TextDelta))
    want = GroundTruth(plan).balance("priya/checking")
    assert f"{want:,}" in reply or str(want) in reply, reply
    used = [
        b["name"]
        for m in conversation.history
        if m["role"] == "assistant"
        for b in m["content"]
        if b["type"] == "tool_use"
    ]
    assert "get_accounts" in used
    rows = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    assert len(rows) >= 2 and all("error" not in r["meta"] for r in rows)
    assert all(r["meta"]["session_id"] == conversation.session_id for r in rows)
    cached = sum(
        r["units"].get("cache_read_input_tokens", 0)
        + r["units"].get("cache_creation_input_tokens", 0)
        for r in rows
    )
    assert cached > 0, f"the prompt was never cached: {[r['units'] for r in rows]}"
