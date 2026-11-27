"""M1e: the agent's tools, the conversation engine and the terminal client.

Claude is replaced by a scripted model, so these tests are free and deterministic.
The bank is the in-memory fake seeded with the real plan (seed 42), and answers are
checked against the plan's ground truth.

Safety properties checked here (each mutation-checked):
- a user message reaches the SafetySession before the model, and a confirming "yes"
  never reaches the model;
- a successful proposal ends the turn with the code-written read-back, with nothing
  yielded in between (a client that stops reading can't leave an unseen transfer
  pending), and nothing else runs after it;
- the request always turns parallel tool calls off;
- every model call is in the cost ledger, failures included, before its content is
  converted (F-15).
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import pytest
from nessie_tools import AuditLog, BankError, TransferStatus

from ledgervoice.agent.chat import ChatSetup, build_conversation, render, run_chat
from ledgervoice.agent.engine import CALL_LIMIT, SORRY, Conversation
from ledgervoice.agent.events import (
    ReadBack,
    RuntimeReply,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnEnded,
)
from ledgervoice.agent.model import AnthropicModel, ModelResponse, TextChunk
from ledgervoice.agent.prompt import system_prompt
from ledgervoice.agent.tools import TOOL_SCHEMAS, BankTools
from ledgervoice.costs import CostLedger
from ledgervoice.seed.apply import seed_fake
from ledgervoice.seed.generator import generate
from ledgervoice.seed.truth import GroundTruth

ANCHOR = date(2026, 11, 24)
S = TransferStatus


# --- a scripted model ----------------------------------------------------------------
@dataclass
class Step:
    """One scripted model response: text chunks, then the content blocks."""

    text: list[str] = field(default_factory=list)
    tools: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fail: bool = False


class ScriptedModel:
    def __init__(self, steps: list[Step]) -> None:
        self.steps = list(steps)
        self.calls: list[list[dict[str, Any]]] = []  # the messages of each call

    def stream(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Iterator[TextChunk | ModelResponse]:
        self.calls.append(json.loads(json.dumps(messages)))
        assert tools == TOOL_SCHEMAS and "LedgerVoice" in system
        if not self.steps:
            raise AssertionError("the model was called more often than scripted")
        step = self.steps.pop(0)
        if step.fail:
            raise ConnectionError("network down")
        for chunk in step.text:
            yield TextChunk(chunk)
        content: list[dict[str, Any]] = []
        if step.text:
            content.append({"type": "text", "text": "".join(step.text)})
        for i, (name, tool_input) in enumerate(step.tools):
            content.append(
                {
                    "type": "tool_use",
                    "id": f"tu_{len(self.calls)}_{i}",
                    "name": name,
                    "input": tool_input,
                }
            )
        yield ModelResponse(content, "tool_use" if step.tools else "end_turn", {})


@dataclass
class World:
    conversation: Conversation
    model: ScriptedModel
    bank: Any
    audit: AuditLog
    manifest: Any
    truth: GroundTruth

    def account(self, key: str) -> Any:
        return self.bank.get_account(self.manifest.accounts[key])

    def last4(self, key: str) -> str:
        return str(self.account(key).last4)

    def send(self, text: str, source: str = "typed") -> list[Any]:
        events = list(self.conversation.send(text, source=source))  # type: ignore[arg-type]
        assert_valid_history(self.conversation.history)
        return events

    def balance(self, key: str) -> int:
        from nessie_tools import compute_balance

        return compute_balance(self.bank, self.manifest.accounts[key]).current


@pytest.fixture(scope="module")
def plan():
    return generate(42, ANCHOR)


def make_world(plan, steps: list[Step], persona: str = "priya") -> World:
    bank, manifest = seed_fake(plan)
    audit = AuditLog(":memory:")
    model = ScriptedModel(steps)
    conversation, _ = build_conversation(
        bank, audit, manifest.customers[persona], ANCHOR, lambda _sid: model
    )
    return World(conversation, model, bank, audit, manifest, GroundTruth(plan))


def assert_valid_history(history: list[dict[str, Any]]) -> None:
    """What the API requires: alternating roles starting with the user, no empty text,
    and every tool_use answered by a tool_result in the next message."""
    assert history and history[0]["role"] == "user"
    for i, message in enumerate(history):
        assert message["role"] == ("user" if i % 2 == 0 else "assistant"), i
        assert message["content"], i
        for block in message["content"]:
            if block["type"] == "text":
                assert block["text"], i
        uses = [b["id"] for b in message["content"] if b["type"] == "tool_use"]
        if uses:
            answered = [
                b["tool_use_id"] for b in history[i + 1]["content"] if b["type"] == "tool_result"
            ]
            assert sorted(answered) == sorted(uses), i
    assert history[-1]["role"] == "assistant"


def tool_results(message: dict[str, Any]) -> list[dict[str, Any]]:
    return [b for b in message["content"] if b["type"] == "tool_result"]


# --- answers come from the tools ----------------------------------------------------------
def test_a_question_is_answered_from_a_tool(plan) -> None:
    w = make_world(plan, [Step(tools=[("get_accounts", {})]), Step(text=["You have ", "$X."])])
    events = w.send("What's my checking balance?")
    kinds = [type(e).__name__ for e in events]
    assert kinds == ["ToolStarted", "ToolFinished", "TextDelta", "TextDelta", "TurnEnded"]
    assert events[-1] == TurnEnded("model", 2, events[-1].first_output_ms)
    (result,) = tool_results(w.conversation.history[2])
    accounts = {a["last4"]: a for a in json.loads(result["content"])["accounts"]}
    assert accounts[w.last4("priya/checking")]["balance"] == w.truth.balance("priya/checking")
    assert accounts[w.last4("priya/card")]["amount_owed"] == w.truth.amount_owed("priya/card")


def test_the_model_sees_the_tool_result_on_its_next_call(plan) -> None:
    w = make_world(plan, [Step(tools=[("get_bills", {})]), Step(text=["ok"])])
    w.send("any bills?")
    second = w.model.calls[1]
    assert second[-1]["role"] == "user" and tool_results(second[-1])


# --- the safety wiring ----------------------------------------------------------------------
def propose_step(w: World, amount: str = "50") -> Step:
    return Step(
        text=["Sure."],
        tools=[
            (
                "propose_transfer",
                {
                    "from_last4": w.last4("priya/checking"),
                    "to_last4": w.last4("priya/savings"),
                    "amount": amount,
                },
            )
        ],
    )


def test_a_proposal_ends_the_turn_with_the_code_written_read_back(plan) -> None:
    w = make_world(plan, [])
    w.model.steps = [propose_step(w), Step(text=["this must never be requested"])]
    events = w.send("move 50 to savings")
    read_back = [e for e in events if isinstance(e, ReadBack)]
    assert len(read_back) == 1 and read_back[0].text.startswith("You're moving $50 from checking")
    assert events[-1].reason == "read_back" and len(w.model.calls) == 1
    # the model will see exactly what the user saw
    assert w.conversation.history[-1]["content"] == [{"type": "text", "text": read_back[0].text}]
    assert w.audit.get(read_back[0].transfer_id).status is S.PENDING


def test_a_yes_executes_without_reaching_the_model(plan) -> None:
    w = make_world(plan, [])
    w.model.steps = [propose_step(w)]
    before = (w.balance("priya/checking"), w.balance("priya/savings"))
    w.send("move 50 to savings")
    events = w.send("yes")
    assert len(w.model.calls) == 1  # the yes was never sent to the model
    assert [type(e) for e in events] == [RuntimeReply, TurnEnded]
    assert "moved $50" in events[0].text and events[-1].reason == "confirmation"
    assert (w.balance("priya/checking"), w.balance("priya/savings")) == (
        before[0] - 50,
        before[1] + 50,
    )


def test_anything_else_cancels_and_the_model_gets_an_app_note(plan) -> None:
    w = make_world(plan, [])
    w.model.steps = [propose_step(w), Step(text=["Okay, cancelled."])]
    tid = [e for e in w.send("move 50 to savings") if isinstance(e, ReadBack)][0].transfer_id
    w.send("hmm, actually no")
    sent = w.model.calls[1][-1]["content"]
    assert sent[0]["text"].startswith("[App note: ") and "cancelled" in sent[0]["text"]
    assert sent[1] == {"type": "text", "text": "hmm, actually no"}
    assert w.audit.get(tid).status is S.CANCELLED


def test_nothing_is_yielded_between_the_proposal_and_its_read_back(plan) -> None:
    """A client that stops reading right after the proposal ran must not leave a
    transfer pending that the user never saw."""
    w = make_world(plan, [])
    w.model.steps = [propose_step(w)]
    turn = w.conversation.send("move 50 to savings")
    for event in turn:
        if isinstance(event, ToolStarted) and event.name == "propose_transfer":
            break
    assert w.conversation._safety.pending_transfer_id is None  # not run yet
    nxt = next(turn)
    assert isinstance(nxt, ReadBack), nxt  # the very next event is the read-back
    turn.close()
    assert_valid_history(w.conversation.history)


def test_closing_a_turn_before_the_proposal_runs_leaves_nothing_pending(plan) -> None:
    w = make_world(plan, [])
    w.model.steps = [propose_step(w)]
    turn = w.conversation.send("move 50 to savings")
    for event in turn:
        if isinstance(event, ToolStarted):
            break
    turn.close()
    assert w.conversation._safety.pending_transfer_id is None
    assert w.audit.records() == []
    assert_valid_history(w.conversation.history)


def test_nothing_runs_after_a_proposal_in_the_same_response(plan) -> None:
    w = make_world(plan, [])
    step = propose_step(w)
    step.tools.append(("propose_transfer", dict(step.tools[0][1], amount="70")))
    step.tools.append(("get_accounts", {}))
    w.model.steps = [step]
    events = w.send("move money")
    assert [e.name for e in events if isinstance(e, ToolStarted)] == ["propose_transfer"]
    results = tool_results(w.conversation.history[-2])
    assert [r.get("is_error", False) for r in results] == [False, True, True]
    assert len(w.audit.records()) == 1  # the second proposal never reached the session


def test_a_refused_proposal_goes_back_to_the_model(plan) -> None:
    w = make_world(plan, [])
    w.model.steps = [propose_step(w, amount="5000"), Step(text=["That's over the limit."])]
    events = w.send("move 5000 to savings")
    assert not [e for e in events if isinstance(e, ReadBack)]
    assert ToolFinished("propose_transfer", False) in events
    (result,) = tool_results(w.model.calls[1][-1])
    assert result["is_error"] and "$2,000" in result["content"]


def test_an_internal_error_while_proposing_is_reported_and_nothing_waits(plan, monkeypatch) -> None:
    w = make_world(plan, [])
    w.model.steps = [propose_step(w), Step(text=["Sorry."])]

    def broken(*_a, **_k):
        raise RuntimeError("audit db locked")

    monkeypatch.setattr(w.conversation._safety, "propose", broken)
    w.send("move 50 to savings")
    (result,) = tool_results(w.model.calls[1][-1])
    assert result["is_error"] and "nothing was moved" in result["content"]
    assert w.conversation._safety.pending_transfer_id is None


# --- limits and failures ----------------------------------------------------------------------
def test_a_turn_stops_after_five_model_calls(plan) -> None:
    w = make_world(plan, [Step(tools=[("get_accounts", {})]) for _ in range(6)])
    events = w.send("loop forever")
    assert events[-2] == RuntimeReply(CALL_LIMIT)
    assert events[-1].reason == "call_limit" and len(w.model.calls) == 5


def test_a_failed_model_call_gets_a_fixed_reply_and_a_valid_history(plan) -> None:
    w = make_world(plan, [Step(fail=True), Step(text=["fine now"])])
    events = w.send("hello")
    assert events[-2] == RuntimeReply(SORRY) and events[-1].reason == "error"
    w.send("hello again")  # the conversation carries on
    assert w.model.calls[1][-1]["content"] == [{"type": "text", "text": "hello again"}]


def test_stopping_mid_turn_keeps_the_history_valid(plan) -> None:
    w = make_world(plan, [Step(tools=[("get_accounts", {})]), Step(text=["x"])])
    turn = w.conversation.send("balances?")
    next(turn)  # ToolStarted
    turn.close()
    assert_valid_history(w.conversation.history)
    assert w.conversation.turns[-1].reason == "error"


def test_turns_record_their_source_and_first_output(plan) -> None:
    w = make_world(plan, [Step(text=["hi"])])
    w.send("hello", source="spoken")
    record = w.conversation.turns[-1]
    assert record.source == "spoken" and record.reason == "model"
    assert record.first_output_ms is not None


# --- the tools on their own --------------------------------------------------------------------
@pytest.fixture
def tools(plan):
    w = make_world(plan, [])
    return w, BankTools(w.bank, w.manifest.customers["priya"], w.conversation._safety)


def run(t: BankTools, name: str, **tool_input: Any) -> dict[str, Any]:
    outcome = t.run(name, tool_input)
    assert not outcome.is_error, outcome.content
    return dict(json.loads(outcome.content))


def test_spending_totals_match_the_ground_truth(tools) -> None:
    w, t = tools
    start, end = w.truth.last_n_days(30)
    got = run(t, "get_transactions", start=start.isoformat(), end=end.isoformat())
    want = w.truth.spend("priya", start, end)
    assert (got["total"], got["count"]) == (want.total, want.count)
    assert got["by_category"] == w.truth.spend_by_category("priya", start, end)
    assert len(got["items"]) == got["items_shown"] == min(want.count, 20)
    for category in got["by_category"]:
        one = run(
            t, "get_transactions", start=start.isoformat(), end=end.isoformat(), category=category
        )
        assert one["total"] == w.truth.spend("priya", start, end, category=category).total


def test_income_excludes_the_apps_own_transfers(tools) -> None:
    w, t = tools
    service = w.conversation._safety._service
    customer = w.manifest.customers["priya"]
    checking = w.manifest.accounts["priya/checking"]
    pending = service.create_pending(customer, w.manifest.accounts["priya/savings"], checking, 20)
    service.execute_pending(pending.transfer_id, customer_id=customer)
    (leg,) = [d for d in w.bank.list_deposits(checking) if pending.transfer_id in d.description]
    start = ANCHOR - timedelta(days=60)
    end = max(ANCHOR, leg.transaction_date)  # the leg is inside the window
    got = run(t, "get_transactions", start=start.isoformat(), end=end.isoformat(), kind="income")
    assert got["total"] == w.truth.income("priya", start, end)  # the move is not income
    assert all(pending.transfer_id not in i["description"] for i in got["items"])


def test_a_purchase_at_an_unknown_merchant_is_counted_as_unknown(tools) -> None:
    w, t = tools
    w.bank.create_purchase(
        w.manifest.accounts["priya/checking"], "no-such-merchant", 7, ANCHOR, "?"
    )
    got = run(t, "get_transactions", start=ANCHOR.isoformat(), end=ANCHOR.isoformat())
    assert got["by_category"].get("unknown") == 7
    assert any(i["merchant"] == "unknown" and i["amount"] == 7 for i in got["items"])


def test_merchant_names_match_by_substring_or_suggest_the_closest(tools, plan) -> None:
    w, t = tools
    name = next(
        plan.merchant(tx.merchant_key).name
        for tx in plan.transactions
        if tx.account_key.startswith("priya/") and tx.merchant_key
    )
    span = {"start": (ANCHOR - timedelta(days=90)).isoformat(), "end": ANCHOR.isoformat()}
    hit = run(t, "get_transactions", **span, merchant=name.split()[0].lower())
    assert name in hit["matched_merchants"] and hit["count"] >= 1
    miss = run(t, "get_transactions", **span, merchant="zzzz nothing")
    assert miss["matched_merchants"] == [] and len(miss["closest_merchants"]) == 3


@pytest.mark.parametrize(
    ("name", "tool_input", "says"),
    [
        ("get_transactions", {"start": "2026-11-30", "end": "2026-11-01"}, "on or before"),
        ("get_transactions", {"start": "yesterday", "end": "2026-11-01"}, "date like"),
        ("get_transactions", {"start": "2026-11-01"}, "date like"),
        ("get_transactions", {"start": "2026-11-01", "end": "2026-11-02", "kind": "x"}, "kind"),
        (
            "get_transactions",
            {"start": "2026-11-01", "end": "2026-11-02", "category": "x"},
            "category",
        ),
        (
            "get_transactions",
            {"start": "2026-11-01", "end": "2026-11-02", "account": "0000"},
            "no account",
        ),
        (
            "get_transactions",
            {"start": "2026-11-01", "end": "2026-11-02", "bogus": 1},
            "unexpected",
        ),
        ("get_bills", {"status": "late"}, "status must be"),
        ("get_accounts", {"x": 1}, "unexpected"),
        ("delete_everything", {}, "no tool"),
    ],
)
def test_bad_tool_input_comes_back_as_an_error(tools, name, tool_input, says) -> None:
    _, t = tools
    outcome = t.run(name, tool_input)
    assert outcome.is_error and says in outcome.content


def test_tool_input_that_is_not_an_object_is_refused(tools) -> None:
    _, t = tools
    assert t.run("get_accounts", ["x"]).is_error


def test_a_bank_error_becomes_a_message_for_the_model(tools, monkeypatch) -> None:
    w, t = tools

    def down(_customer_id):
        raise BankError("503")

    monkeypatch.setattr(w.bank, "list_accounts", down)
    outcome = t.run("get_accounts", {})
    assert outcome.is_error and "could not be reached" in outcome.content


# --- the request and the cost ledger ------------------------------------------------------------
class FakeStream:
    def __init__(self, events, final, fail_at: int | None = None) -> None:
        self._events, self._final, self._fail_at = events, final, fail_at

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for i, e in enumerate(self._events):
            if self._fail_at == i:
                raise ConnectionError("dropped")
            yield e

    def get_final_message(self):
        return self._final


@dataclass
class _Obj:
    kw: dict[str, Any]

    def __getattr__(self, name):
        try:
            return self.kw[name]
        except KeyError:
            raise AttributeError(name) from None


def sdk_client(stream: FakeStream, seen: list[dict[str, Any]]):
    class Messages:
        def stream(self, **kwargs):
            seen.append(kwargs)
            return stream

    return _Obj({"messages": Messages()})


def final_message(content, stop="end_turn"):
    usage = _Obj(
        {
            "input_tokens": 900,
            "output_tokens": 20,
            "cache_read_input_tokens": 600,
            "cache_creation_input_tokens": 0,
        }
    )
    return _Obj({"content": content, "stop_reason": stop, "usage": usage})


def test_the_request_turns_parallel_tools_off_and_caches(tmp_path) -> None:
    seen: list[dict[str, Any]] = []
    stream = FakeStream([_Obj({"type": "text", "text": "hi"})], final_message([]))
    model = AnthropicModel(
        sdk_client(stream, seen),
        model="claude-haiku-5-5",
        effort="low",
        ledger=CostLedger(tmp_path / "l.jsonl"),
        session_id="s1",
    )
    list(model.stream(system="sys", messages=[], tools=TOOL_SCHEMAS))
    (kwargs,) = seen
    assert kwargs["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert kwargs["cache_control"] == {"type": "ephemeral"}
    assert kwargs["output_config"] == {"effort": "low"}
    assert kwargs["model"] == "claude-haiku-5-5" and kwargs["tools"] == TOOL_SCHEMAS


def test_every_model_call_is_in_the_ledger_with_its_session(tmp_path) -> None:
    ledger_path = tmp_path / "l.jsonl"
    text = _Obj({"type": "text", "text": "Hello"})
    use = _Obj({"type": "tool_use", "id": "t1", "name": "get_accounts", "input": {}})
    stream = FakeStream(
        [_Obj({"type": "text", "text": "Hel"})], final_message([text, use], "tool_use")
    )
    model = AnthropicModel(
        sdk_client(stream, []),
        model="claude-haiku-5-5",
        effort="low",
        ledger=CostLedger(ledger_path),
        session_id="sess-1",
    )
    items = list(model.stream(system="s", messages=[], tools=[]))
    assert items[0] == TextChunk("Hel")
    assert items[-1] == ModelResponse(
        [
            {"type": "text", "text": "Hello"},
            {"type": "tool_use", "id": "t1", "name": "get_accounts", "input": {}},
        ],
        "tool_use",
        {
            "input_tokens": 900,
            "output_tokens": 20,
            "cache_read_input_tokens": 600,
            "cache_creation_input_tokens": 0,
        },
    )
    (row,) = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    assert row["service"] == "anthropic/claude-haiku-5-5"
    assert row["meta"]["session_id"] == "sess-1" and row["meta"]["ttft_ms"] is not None
    assert row["unpriced_units"] == [] and row["est_usd"] > 0  # priced (D63 / PRICING)


def test_a_failed_model_call_is_still_in_the_ledger(tmp_path) -> None:
    ledger_path = tmp_path / "l.jsonl"
    stream = FakeStream([_Obj({"type": "text", "text": "x"})], final_message([]), fail_at=0)
    model = AnthropicModel(
        sdk_client(stream, []),
        model="claude-haiku-5-5",
        effort="low",
        ledger=CostLedger(ledger_path),
        session_id="s",
    )
    with pytest.raises(ConnectionError):
        list(model.stream(system="s", messages=[], tools=[]))
    (row,) = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    assert row["meta"]["error"] == "ConnectionError"


def test_the_call_is_recorded_before_its_content_is_converted(tmp_path) -> None:
    """F-15: a response we can't convert was still billed."""
    ledger_path = tmp_path / "l.jsonl"
    broken = _Obj({"type": "text"})  # no .text attribute: conversion raises
    stream = FakeStream([], final_message([broken]))
    model = AnthropicModel(
        sdk_client(stream, []),
        model="claude-haiku-5-5",
        effort="low",
        ledger=CostLedger(ledger_path),
        session_id="s",
    )
    with pytest.raises(AttributeError):
        list(model.stream(system="s", messages=[], tools=[]))
    assert len(ledger_path.read_text().splitlines()) == 1


# --- the prompt and the terminal client ----------------------------------------
def test_the_system_prompt_carries_the_name_and_date() -> None:
    text = system_prompt("Priya", ANCHOR)
    assert "Priya" in text and "Tuesday, 2026-11-24" in text and "[App note:" in text


def test_the_terminal_chat_shows_the_read_back_and_quits(plan) -> None:
    w = make_world(plan, [])
    w.model.steps = [propose_step(w)]
    lines = iter(["move 50 to savings", "yes", "quit"])
    out = io.StringIO()
    setup = ChatSetup(w.conversation, "Priya", ANCHOR, "test bank", lambda: None)
    assert run_chat(setup, read=lambda _p: next(lines), out=out) == 0
    text = out.getvalue()
    assert ">>> You're moving $50 from checking" in text and "moved $50" in text


def test_render_prints_text_tools_and_timing() -> None:
    out = io.StringIO()
    render(
        iter(
            [
                TextDelta("Hi"),
                ToolStarted("get_accounts"),
                TextDelta("Done"),
                TurnEnded("model", 2, 12.0),
            ]
        ),
        out,
        timing=True,
    )
    assert (
        out.getvalue() == "Hi\n  (checking your accounts...)\nDone\n"
        "  [model; 2 model call(s); first output 12 ms]\n"
    )
