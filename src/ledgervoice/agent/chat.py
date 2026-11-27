"""`make chat` / `make chat-live`: talk to the agent in the terminal.

This is only a client of the engine (D58): it builds one Conversation and prints its
events. The M3 web frontend will build Conversations the same way.

- `make chat` (D61): an in-memory fake bank seeded with the same plan as the live
  seed (seed 42 by default, dated today or LEDGERVOICE_TODAY). Free, deterministic,
  and transfers never touch the Nessie sandbox; everything is gone when you quit.
- `make chat-live`: the live seed recorded in .data/seed (run `make seed-live` first).
  Transfers are real Nessie writes, logged in that seed's audit database.

Claude is called in both, so both cost (a little) money; every call is in the cost
ledger with this conversation's session id.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TextIO

from nessie_tools import AuditLog, BankBackend, TransferService

from ledgervoice.agent.engine import Conversation
from ledgervoice.agent.events import (
    Event,
    ReadBack,
    RuntimeReply,
    TextDelta,
    ToolStarted,
    TurnEnded,
)
from ledgervoice.agent.model import AnthropicModel, ModelClient
from ledgervoice.agent.prompt import system_prompt
from ledgervoice.agent.tools import BankTools
from ledgervoice.safety import SafetySession
from ledgervoice.seed.catalog import PERSONAS

PERSONA_KEYS = tuple(p.key for p in PERSONAS)
TOOL_LABELS = {
    "get_accounts": "checking your accounts",
    "get_transactions": "looking at your transactions",
    "get_bills": "looking at your bills",
    "propose_transfer": "setting up the transfer",
}


@dataclass
class ChatSetup:
    conversation: Conversation
    customer_name: str
    today: date
    bank_label: str
    close: Callable[[], None]


def build_conversation(
    backend: BankBackend,
    audit: AuditLog,
    customer_id: str,
    today: date,
    make_model: Callable[[str], ModelClient],
) -> tuple[Conversation, str]:
    """Wire one conversation: safety session -> tools -> model -> engine. Everything
    is per conversation; nothing is shared but the bank and the audit log."""
    service = TransferService(backend, audit)
    safety = SafetySession(backend, service, audit, customer_id)
    tools = BankTools(backend, customer_id, safety)
    name = backend.get_customer(customer_id).first_name
    conversation = Conversation(
        make_model(safety.session_id), tools, safety, system_prompt(name, today)
    )
    return conversation, name


def anthropic_model_factory() -> Callable[[str], ModelClient]:
    import anthropic

    from ledgervoice.config import get_settings
    from ledgervoice.costs import CostLedger

    settings = get_settings()
    settings.require("anthropic_api_key")
    assert settings.anthropic_api_key is not None
    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key.get_secret_value(), timeout=30.0
    )
    ledger = CostLedger(settings.cost_ledger_path)

    def make(session_id: str) -> ModelClient:
        return AnthropicModel(
            client,
            model=settings.claude_model,
            effort=settings.claude_effort,
            ledger=ledger,
            session_id=session_id,
        )

    return make


def setup_fake(
    persona: str, seed: int, anchor: date, make_model: Callable[[str], ModelClient]
) -> ChatSetup:
    from ledgervoice.seed.apply import seed_fake
    from ledgervoice.seed.generator import generate

    bank, manifest = seed_fake(generate(seed, anchor))
    audit = AuditLog(":memory:")
    conversation, name = build_conversation(
        bank, audit, manifest.customers[persona], anchor, make_model
    )
    return ChatSetup(conversation, name, anchor, f"in-memory demo bank (seed {seed})", audit.close)


def setup_live(
    persona: str, today: date | None, make_model: Callable[[str], ModelClient]
) -> ChatSetup:
    from ledgervoice.config import get_settings
    from ledgervoice.livebank import open_live_bank
    from ledgervoice.seed.manifest import SeedStatus, SeedStore

    manifest = SeedStore(get_settings().seed_dir).load_manifest()
    if manifest is None or manifest.status is not SeedStatus.COMPLETE:
        raise SystemExit("no complete live seed found; run `make seed-live` first")
    if not manifest.audit_db or not Path(manifest.audit_db).exists():
        raise SystemExit(f"the seed's audit database {manifest.audit_db} is missing")
    backend, server, close_bank = open_live_bank()
    if server != manifest.server:
        close_bank()
        raise SystemExit(f"the live seed was made on {manifest.server}, not {server}")
    audit = AuditLog(manifest.audit_db)
    day = today or manifest.anchor  # D52: the seed's date unless LEDGERVOICE_TODAY is set
    conversation, name = build_conversation(
        backend, audit, manifest.customers[persona], day, make_model
    )

    def close() -> None:
        audit.close()
        close_bank()

    return ChatSetup(conversation, name, day, "live Nessie sandbox seed", close)


def render(events: Iterator[Event], out: TextIO, *, timing: bool = False) -> None:
    """Print one turn's events."""
    mid_line = False
    for event in events:
        if isinstance(event, TextDelta):
            out.write(event.text)
            mid_line = True
        elif isinstance(event, ToolStarted):
            out.write(
                ("\n" if mid_line else "") + f"  ({TOOL_LABELS.get(event.name, event.name)}...)\n"
            )
            mid_line = False
        elif isinstance(event, ReadBack):
            out.write(("\n" if mid_line else "") + f"  >>> {event.text}\n")
            mid_line = False
        elif isinstance(event, RuntimeReply):
            out.write(("\n" if mid_line else "") + event.text + "\n")
            mid_line = False
        elif isinstance(event, TurnEnded):
            if mid_line:
                out.write("\n")
            if timing:
                ms = "-" if event.first_output_ms is None else f"{event.first_output_ms:.0f} ms"
                out.write(
                    f"  [{event.reason}; {event.model_calls} model call(s); first output {ms}]\n"
                )
        out.flush()


def run_chat(
    setup: ChatSetup,
    *,
    read: Callable[[str], str] = input,
    out: TextIO = sys.stdout,
    timing: bool = False,
) -> int:
    out.write(
        f"LedgerVoice: chatting as {setup.customer_name} ({setup.bank_label}; "
        f"today is {setup.today.isoformat()}). Type 'quit' to leave.\n"
    )
    while True:
        try:
            text = read("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            out.write("\n")
            return 0
        if not text:
            continue
        if text.lower() in {"quit", "exit"}:
            return 0
        out.write("agent> ")
        turn = setup.conversation.send(text, source="typed")
        try:
            render(turn, out, timing=timing)
        except KeyboardInterrupt:
            turn.close()  # the engine keeps its history valid
            out.write("\n(interrupted)\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ledgervoice.agent")
    parser.add_argument("--live", action="store_true", help="use the live seed (make seed-live)")
    parser.add_argument("--persona", choices=PERSONA_KEYS, default="priya")
    parser.add_argument("--seed", type=int, default=42, help="fake bank only")
    parser.add_argument("--anchor", type=date.fromisoformat, help="fake bank only: seed date")
    parser.add_argument("--timing", action="store_true", help="print time to first output")
    args = parser.parse_args(argv)

    from ledgervoice.config import get_settings

    settings = get_settings()
    make_model = anthropic_model_factory()
    if args.live:
        setup = setup_live(args.persona, settings.ledgervoice_today, make_model)
    else:
        anchor = args.anchor or settings.ledgervoice_today or date.today()
        setup = setup_fake(args.persona, args.seed, anchor, make_model)
    try:
        return run_chat(setup, timing=args.timing)
    finally:
        setup.close()
