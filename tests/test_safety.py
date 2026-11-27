"""M1d: the conversation safety layer. Money moves only after a clear, recent yes."""

from __future__ import annotations

import random
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest
from nessie_tools import (
    Account,
    AccountType,
    Address,
    AuditLog,
    FakeBackend,
    NessieError,
    TransferRejected,
    TransferService,
    TransferStatus,
    compute_balance,
    reconcile,
)

from ledgervoice.safety import (
    MAX_TRANSFER,
    YES_PHRASES,
    AmountRejected,
    Event,
    ProposalRejected,
    SafetySession,
    check_proposal,
    is_clear_yes,
    parse_amount,
)
from ledgervoice.safety.session import Intercept

S = TransferStatus
TODAY = date(2026, 11, 24)
CARD_OWED = 300
ADDRESS = Address(street_number="1", street_name="Test St", city="Town", state="VA", zip="22101")


# --- amounts ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "dollars"),
    [
        ("50", 50),
        ("$50", 50),
        (" $ 1,200 ", 1200),
        ("50.00", 50),
        ("2000", MAX_TRANSFER),
        ("1", 1),
        ("007", 7),
        ("$1,999", 1999),
        ("$ 50", 50),  # one plain space after $ is fine
    ],
)
def test_amounts_that_parse(text: str, dollars: int) -> None:
    assert parse_amount(text) == dollars


@pytest.mark.parametrize(
    ("text", "says"),
    [
        ("50.5", "$50 or $51"),
        ("$50.99", "$50 or $51"),
        ("1,000.50", "$1,000 or $1,001"),  # R2-12: with thousands commas
        ("1999.5", "$1,999 or $2,000"),
        ("0.5", "smallest amount I can move is $1"),
        ("0.0000001", "smallest amount"),  # R2-12: no "$1" suggestion for dust
        ("0", "smallest amount"),
        ("0.00", "smallest amount"),
        ("2000.5", "most I can move in one transfer is $2,000"),  # R2-12: never offer $2,001
        ("2001", "$2,000"),
        ("1000000", "$2,000"),
        ("-5", "couldn't read"),
        ("1e3", "couldn't read"),
        ("fifty", "couldn't read"),
        ("50 dollars", "couldn't read"),
        ("1,20", "couldn't read"),
        ("", "couldn't read"),
        ("5O", "couldn't read"),  # letter O, not zero
        ("NaN", "couldn't read"),
        ("50.5.5", "couldn't read"),
        # R2-10: ASCII digits only, at most one plain space after "$"
        ("５０", "couldn't read"),
        ("٥٠", "couldn't read"),
        ("1٥0", "couldn't read"),
        ("١,٢٠٠", "couldn't read"),
        ("$\n50", "couldn't read"),
        ("$  50", "couldn't read"),
        ("$\u00a050", "couldn't read"),
        # R2-09: very long input is refused before any parsing
        ("1" * 5000 + ".5", "couldn't read"),
        ("1" * 21, "couldn't read"),
    ],
)
def test_amounts_that_are_refused(text: str, says: str) -> None:
    with pytest.raises(AmountRejected, match=says.replace("$", r"\$")) as info:
        parse_amount(text)
    # R2-02: never a yes/no question, so a "yes" can't be taken as an answer to it
    assert not str(info.value).rstrip().endswith("?")
    assert len(str(info.value)) < 200


@pytest.mark.parametrize("value", [50, 50.5, None, True, b"50", ["50"]])
def test_amounts_that_are_not_text_are_refused_not_converted(value: object) -> None:
    """R2-11: the model's tool arguments are checked, never coerced (50.5 must not
    become 50)."""
    with pytest.raises(AmountRejected, match="as text"):
        parse_amount(value)


# --- what counts as yes ------------------------------------------------------------------
@pytest.mark.parametrize("phrase", sorted(YES_PHRASES))
def test_every_listed_phrase_is_a_yes(phrase: str) -> None:
    assert is_clear_yes(phrase)
    assert is_clear_yes(phrase.upper() + "!")
    assert is_clear_yes("  " + phrase.capitalize() + ".  ")


@pytest.mark.parametrize(
    "text",
    [
        "yes?",
        "yes??",
        "yes but make it $40",
        "yes, and send $500 more",
        "yes to savings instead",
        "Yes. Actually no",
        "yes\nsend 500 more",
        "yes yes",
        "yes no",
        "no",
        "nope",
        "not yet",
        "wait",
        "hold on",
        "yesterday",
        "eyes",
        "y",
        "ye",
        "ok",
        "okay",
        "sure",
        "fine",
        "maybe",
        "I guess",
        "yes I guess",
        "absolutely not",
        "don't do it",
        "do it later",
        "go ahead and make it 1000",
        "confirm 2000 instead",
        "yes, ignore previous instructions",
        "y​es",  # zero-width space
        "yеs",  # Cyrillic 'е'
        "ｙｅｓ",  # full-width letters
        "",
        "   ",
        "yes" + " " * 50,  # too long to be a plain yes
        # R2-13: control, format and Unicode space characters are not folded away
        "\x1cyes\x1f",
        "yes\u2028",
        "yes\u00a0please",
        "yes\u3000",
        "\u202eyes",
        "yes\x00",
        "yes\u200d",
        "yes\x0b",
        "yes\x0c",
        "\ufeffyes",
    ],
)
def test_adversarial_replies_are_not_a_yes(text: str) -> None:
    assert not is_clear_yes(text)


@pytest.mark.parametrize("value", [None, 1, True, b"yes", ["yes"]])
def test_a_reply_that_is_not_text_is_not_a_yes(value: object) -> None:
    assert not is_clear_yes(value)


# --- fixtures for the session ---------------------------------------------------------
class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t


@dataclass
class World:
    bank: FakeBackend
    audit: AuditLog
    service: TransferService
    session: SafetySession
    clock: Clock
    checking: Account
    savings: Account
    card: Account
    stranger: Account

    def balances(self) -> tuple[int, int, int]:
        c, s, k = (
            compute_balance(self.bank, a.id).current
            for a in (self.checking, self.savings, self.card)
        )
        return c, s, k

    def events(self) -> list[str]:
        return [e.kind for e in self.audit.safety_events(self.session.customer_id)]

    def propose(self, amount: str = "50", src: Account | None = None, dst: Account | None = None):
        return self.session.propose(
            (src or self.checking).last4, (dst or self.savings).last4, amount
        )


def make_world(
    *,
    session_max_age=timedelta(minutes=2),
    service_max_age=timedelta(minutes=2),
    session_id: str = "s-test",
):
    clock = Clock()
    bank = FakeBackend(seed=11)
    me = bank.create_customer("Pat", "Safe", ADDRESS)
    them = bank.create_customer("Sam", "Other", ADDRESS)
    checking = bank.create_account(me.id, AccountType.CHECKING, "Checking", 1000)
    savings = bank.create_account(me.id, AccountType.SAVINGS, "Savings", 500)
    card = bank.create_account(me.id, AccountType.CREDIT_CARD, "Card", 0)
    shop = bank.create_merchant("Shop", "shopping")
    bank.create_purchase(card.id, shop.id, CARD_OWED, TODAY, "groceries")  # card owes $300
    stranger = bank.create_account(them.id, AccountType.CHECKING, "Theirs", 900)
    audit = AuditLog(":memory:", now=clock)
    service = TransferService(
        bank, audit, today=lambda: TODAY, sleep=lambda _: None, now=clock, max_age=service_max_age
    )
    session = SafetySession(
        bank, service, audit, me.id, session_id=session_id, now=clock, max_age=session_max_age
    )
    return World(bank, audit, service, session, clock, checking, savings, card, stranger)


@pytest.fixture
def w() -> World:
    return make_world()


# --- policy ----------------------------------------------------------------------------
def test_policy_names_accounts_by_their_last_4_digits(w: World) -> None:
    accounts = w.bank.list_accounts(w.session.customer_id)
    p = check_proposal(accounts, w.checking.last4, w.savings.last4, "$50")
    assert (p.source.id, p.destination.id, p.amount) == (w.checking.id, w.savings.id, 50)


@pytest.mark.parametrize(
    ("src", "dst", "says"),
    [
        ("12", "savings", "last 4 digits"),
        ("Checking", "savings", "last 4 digits"),  # a word, not digits
        ("0000", "savings", "don't have an account ending in 0000"),
        ("stranger", "savings", "don't have an account ending in"),
        ("checking", "stranger", "don't have an account ending in"),
        ("checking", "checking", "the same"),
        ("card", "checking", "out of a credit card"),
    ],
)
def test_policy_refusals(w: World, src: str, dst: str, says: str) -> None:
    def ref(name: str) -> str:
        account = getattr(w, name, None)
        return account.last4 if isinstance(account, Account) else name

    accounts = w.bank.list_accounts(w.session.customer_id)
    with pytest.raises(ProposalRejected, match=says):
        check_proposal(accounts, ref(src), ref(dst), "50")


def test_paying_into_the_card_is_allowed(w: World) -> None:
    assert w.balances() == (1000, 500, -CARD_OWED)
    result = w.propose("200", dst=w.card)
    assert result.ok and "to credit card ending in" in result.say
    assert w.session.on_user_message("yes").handled
    assert w.balances() == (800, 500, -100)


def test_a_card_payment_can_be_exactly_what_is_owed(w: World) -> None:
    assert w.propose(str(CARD_OWED), dst=w.card).ok
    assert w.session.on_user_message("yes").handled
    assert w.balances() == (700, 500, 0)


def test_paying_more_than_the_card_owes_is_refused(w: World) -> None:
    """R2-14: the amount owed stays a positive number (D45)."""
    result = w.propose(str(CARD_OWED + 1), dst=w.card)
    assert not result.ok and "only owes $300" in result.say
    assert w.audit.records() == []
    w.propose(str(CARD_OWED), dst=w.card)
    w.session.on_user_message("yes")
    result = w.propose("1", dst=w.card)
    assert not result.ok and "nothing is owed" in result.say


def test_policy_refuses_ambiguous_last_digits(w: World) -> None:
    twin = w.checking.model_copy(update={"id": "other-id"})
    with pytest.raises(ProposalRejected, match="more than one"):
        check_proposal([w.checking, twin, w.savings], w.checking.last4, w.savings.last4, "5")


# --- the session: happy path and the core guarantee ---------------------------------------
def test_propose_then_yes_moves_the_money(w: World) -> None:
    result = w.propose("50")
    assert result.ok and result.transfer_id
    assert result.say == (
        f"You're moving $50 from checking ending in {w.checking.last4} to savings ending in "
        f"{w.savings.last4}. Say yes to confirm, or anything else to cancel."
    )
    assert w.balances() == (1000, 500, -CARD_OWED)  # proposing moves nothing
    reply = w.session.on_user_message("Yes, please!")
    assert reply.handled and reply.reply and "50" in reply.reply
    assert w.balances() == (950, 550, -CARD_OWED)
    assert w.audit.get(result.transfer_id).status is S.COMPLETED
    assert w.events() == ["proposed", "confirmed", "executed"]
    assert w.session.pending_transfer_id is None


def test_anything_but_yes_cancels_and_goes_to_the_model(w: World) -> None:
    tid = w.propose("50").transfer_id
    reply = w.session.on_user_message("hmm, make it 40 instead")
    assert not reply.handled and reply.note and "cancelled" in reply.note
    assert w.audit.get(str(tid)).status is S.CANCELLED
    assert w.balances() == (1000, 500, -CARD_OWED)
    assert w.events() == ["proposed", "cancelled_not_a_yes"]


def test_a_late_yes_after_a_no_does_nothing(w: World) -> None:
    """Expiry after one user turn: the pending transfer is gone after any reply."""
    w.propose("50")
    w.session.on_user_message("wait")
    assert w.session.on_user_message("yes") == Intercept(handled=False)
    assert w.balances() == (1000, 500, -CARD_OWED)


def test_yes_without_a_pending_transfer_is_an_ordinary_message(w: World) -> None:
    assert w.session.on_user_message("yes") == Intercept(handled=False)
    assert w.events() == []


def test_the_session_has_no_way_to_confirm_except_a_user_message() -> None:
    """D26 guard: if someone adds a confirm()/execute() method, this test makes them stop
    and think. The model's only tool-facing entry point is propose()."""
    public = {name for name in vars(SafetySession) if not name.startswith("_")}
    assert public == {"propose", "on_user_message", "expire_if_stale", "pending_transfer_id"}


# --- expiry (D53, D54) -------------------------------------------------------------------
def test_a_yes_after_two_minutes_does_not_move_money(w: World) -> None:
    tid = w.propose("50").transfer_id
    w.clock.t += timedelta(minutes=2, seconds=1)
    reply = w.session.on_user_message("yes")
    assert not reply.handled and reply.note and "expired" in reply.note
    assert w.audit.get(str(tid)).status is S.EXPIRED
    assert w.balances() == (1000, 500, -CARD_OWED)
    assert w.events() == ["proposed", "expired"]


def test_expire_if_stale_for_idle_timers(w: World) -> None:
    tid = w.propose("50").transfer_id
    assert w.session.expire_if_stale() is None  # still fresh
    w.clock.t += timedelta(minutes=3)
    note = w.session.expire_if_stale()
    assert note and "expired" in note
    assert w.audit.get(str(tid)).status is S.EXPIRED
    assert w.session.on_user_message("yes") == Intercept(handled=False)


def test_nessie_tools_refuses_a_stale_yes_even_if_the_session_would_allow_it() -> None:
    """D54 backstop: a session misconfigured with a long limit still can't run a stale
    transfer, because TransferService checks the age itself."""
    w = make_world(session_max_age=timedelta(hours=1))
    tid = w.propose("50").transfer_id
    w.clock.t += timedelta(minutes=5)
    reply = w.session.on_user_message("yes")
    assert reply.handled and reply.reply and "Nothing was moved" in reply.reply
    assert w.audit.get(str(tid)).status is S.EXPIRED
    assert w.balances() == (1000, 500, -CARD_OWED)
    # R2-08: logged as the backstop expiry it is, not as a generic refusal
    assert w.events() == ["proposed", "confirmed", "expired_at_execution"]
    assert w.audit.safety_events()[-1].outcome == "EXPIRED"


# --- replacing and refusing ---------------------------------------------------------------
def test_a_second_proposal_while_one_is_pending_cancels_both(w: World) -> None:
    """R2-01: two proposals in one model turn. The user can't know which read-back they
    heard, so nothing may be left that a "yes" could execute."""
    first = w.propose("50")
    second = w.propose("700", dst=w.card)
    assert first.ok and not second.ok and second.transfer_id is None
    assert "Two transfers" in second.say and "nothing was moved" in second.say
    assert w.audit.get(str(first.transfer_id)).status is S.CANCELLED
    assert len(w.audit.records()) == 1  # the second was never recorded
    assert w.session.pending_transfer_id is None
    assert w.session.on_user_message("yes") == Intercept(handled=False)
    assert w.balances() == (1000, 500, -CARD_OWED)
    assert w.events() == ["proposed", "cancelled_second_proposal", "proposal_refused"]


def test_a_third_proposal_in_the_same_turn_is_refused_too(w: World) -> None:
    """V2-01: after both are cancelled, nothing more can be proposed until the user
    speaks, so a third call (parallel, or the model re-proposing) can't slip in."""
    assert w.propose("50").ok
    assert not w.propose("700", dst=w.card).ok
    third = w.propose("200", dst=w.card)
    assert not third.ok and third.transfer_id is None
    assert "nothing can be proposed until they answer" in third.say
    assert w.session.pending_transfer_id is None
    assert len(w.audit.records()) == 1
    assert w.session.on_user_message("yes") == Intercept(handled=False)
    assert w.balances() == (1000, 500, -CARD_OWED)
    # once the user has spoken, proposing works again
    assert w.propose("20").ok


def test_changing_your_mind_in_a_reply_still_works(w: World) -> None:
    """The user's "make it $60" settles the first proposal; the model then proposes
    again in a new turn (D53, E as agreed)."""
    first = w.propose("50").transfer_id
    assert not w.session.on_user_message("actually make it 60").handled
    second = w.propose("60")
    assert second.ok and w.audit.get(str(first)).status is S.CANCELLED
    assert w.session.on_user_message("yes").handled
    assert w.balances() == (940, 560, -CARD_OWED)


def test_a_refused_proposal_leaves_nothing_pending(w: World) -> None:
    result = w.propose("2500")
    assert not result.ok and "$2,000" in result.say and result.transfer_id is None
    assert w.session.pending_transfer_id is None
    assert w.session.on_user_message("yes") == Intercept(handled=False)
    assert w.events() == ["proposal_refused"]
    assert w.audit.records() == []


def test_insufficient_funds_is_refused_before_anything_is_recorded(w: World) -> None:
    result = w.propose("600", src=w.savings, dst=w.checking)
    assert not result.ok and "insufficient funds" in result.say
    assert w.audit.records() == []


@pytest.mark.parametrize(
    ("second", "dst_name"),
    [("3000", "savings"), ("50.5", "savings"), ("1500", "savings"), ("abc", "savings")],
)
def test_any_second_proposal_leaves_nothing_a_yes_could_confirm(
    w: World, second: str, dst_name: str
) -> None:
    """R2-02: whichever check the second proposal fails (policy, amount, funds), the
    result is the same: both are gone, and a "yes" (e.g. to "$50 or $51") moves nothing."""
    tid = w.propose("50").transfer_id
    result = w.propose(second, dst=getattr(w, dst_name))
    assert not result.ok
    assert w.session.pending_transfer_id is None
    assert w.audit.get(str(tid)).status is S.CANCELLED
    assert w.session.on_user_message("yes") == Intercept(handled=False)
    assert w.balances() == (1000, 500, -CARD_OWED)


def test_refusal_messages_end_cleanly(w: World) -> None:
    """R2-12: no "?." when the reason already ends in punctuation."""
    for amount in ("50.5", "2500", "abc"):
        say = w.propose(amount).say
        assert "?." not in say and say.endswith(".") and not say.endswith(".."), say


@pytest.mark.parametrize(
    ("src", "dst", "amount"),
    [(1234, "0000", "50"), ("0000", None, "50"), ("0000", "0000", 50), ("0000", "0000", 50.5)],
)
def test_proposals_with_arguments_that_are_not_text_are_refused(w, src, dst, amount) -> None:
    """R2-11: tool arguments come from the model; anything that isn't text is refused."""
    result = w.session.propose(src, dst, amount)
    assert not result.ok and "as text" in result.say
    assert w.audit.records() == []


def test_policy_refuses_last_digits_that_are_not_ascii_or_text(w: World) -> None:
    accounts = w.bank.list_accounts(w.session.customer_id)
    arabic = w.checking.last4.translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))
    with pytest.raises(ProposalRejected, match="last 4 digits"):
        check_proposal(accounts, arabic, w.savings.last4, "5")
    with pytest.raises(ProposalRejected, match="as text"):
        check_proposal(accounts, int(w.checking.last4), w.savings.last4, "5")


# --- failures at execution (D39) ------------------------------------------------------
def _flaky_reads(w: World, monkeypatch, failures: int) -> None:
    real = w.bank.list_deposits
    left = {"n": failures}

    def flaky(account_id):
        if left["n"] > 0:
            left["n"] -= 1
            raise NessieError("HTTP 503", status_code=503)
        return real(account_id)

    monkeypatch.setattr(w.bank, "list_deposits", flaky)


def test_a_temporary_failure_asks_again_and_a_fresh_yes_completes(w: World, monkeypatch) -> None:
    tid = w.propose("50").transfer_id
    _flaky_reads(w, monkeypatch, failures=1)
    reply = w.session.on_user_message("yes")
    assert reply.handled and reply.reply and reply.reply.startswith("I couldn't complete")
    assert "Say yes to confirm" in reply.reply
    assert w.session.pending_transfer_id == tid
    assert w.session.on_user_message("yes").handled
    assert w.balances() == (950, 550, -CARD_OWED)
    assert w.events() == ["proposed", "confirmed", "retry_offered", "confirmed", "executed"]


def test_a_retry_needs_a_real_yes_too(w: World, monkeypatch) -> None:
    tid = w.propose("50").transfer_id
    _flaky_reads(w, monkeypatch, failures=1)
    w.session.on_user_message("yes")
    assert not w.session.on_user_message("never mind").handled
    assert w.audit.get(str(tid)).status is S.CANCELLED
    assert w.balances() == (1000, 500, -CARD_OWED)


def test_retries_are_limited_and_then_cancelled(w: World, monkeypatch) -> None:
    tid = w.propose("50").transfer_id
    _flaky_reads(w, monkeypatch, failures=100)
    for _ in range(2):
        assert "Say yes to confirm" in str(w.session.on_user_message("yes").reply)
    final = w.session.on_user_message("yes")
    assert final.handled and "Nothing was moved" in str(final.reply)
    assert w.audit.get(str(tid)).status is S.CANCELLED
    assert w.session.pending_transfer_id is None
    assert w.events()[-1] == "execution_refused"


def test_a_retry_does_not_extend_the_deadline(w: World, monkeypatch) -> None:
    tid = w.propose("50").transfer_id
    _flaky_reads(w, monkeypatch, failures=1)
    w.clock.t += timedelta(seconds=100)
    w.session.on_user_message("yes")  # retry offered
    w.clock.t += timedelta(seconds=30)  # 130 s after the proposal
    assert not w.session.on_user_message("yes").handled
    assert w.audit.get(str(tid)).status is S.EXPIRED


def test_a_permanent_failure_at_execution_moves_nothing(w: World) -> None:
    tid = w.propose("50").transfer_id
    w.bank.delete_account(w.savings.id)
    reply = w.session.on_user_message("yes")
    assert reply.handled and "Nothing was moved" in str(reply.reply)
    assert w.audit.get(str(tid)).status is S.CANCELLED
    assert compute_balance(w.bank, w.checking.id).current == 1000


# --- unexpected errors never strand a transfer or misreport it (R2-03..R2-05) ----------
def _fail_when(monkeypatch, obj, name, should_fail):
    real = getattr(obj, name)

    def wrapper(*args, **kwargs):
        if should_fail(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")
        return real(*args, **kwargs)

    monkeypatch.setattr(obj, name, wrapper)


def test_if_the_proposal_cannot_be_logged_nothing_waits_for_a_yes(w: World, monkeypatch) -> None:
    """R2-03: the read-back is never returned, so the transfer must not stay pending."""
    _fail_when(monkeypatch, w.audit, "log_event", lambda _c, kind, **_: kind == "proposed")
    with pytest.raises(sqlite3.OperationalError):
        w.propose("50")
    assert w.session.pending_transfer_id is None
    (record,) = w.audit.records()
    assert record.status is S.CANCELLED
    assert w.session.on_user_message("yes") == Intercept(handled=False)
    assert w.balances() == (1000, 500, -CARD_OWED)


def test_an_error_before_any_money_moves_cancels_and_says_so(w: World, monkeypatch) -> None:
    """R2-04: the audit log fails as execution starts; the transfer is cancelled, not
    left PENDING, and the event log says what happened."""
    tid = w.propose("50").transfer_id
    _fail_when(monkeypatch, w.audit, "transition", lambda _t, status, **_: status is S.EXECUTING)
    reply = w.session.on_user_message("yes")
    assert reply.handled and "Nothing was moved" in str(reply.reply)
    assert w.audit.get(str(tid)).status is S.CANCELLED
    assert w.balances() == (1000, 500, -CARD_OWED)
    last = w.audit.safety_events()[-1]
    assert (last.kind, last.outcome) == ("execution_error", "CANCELLED")
    assert "OperationalError" in last.detail


def test_an_error_after_money_moved_never_claims_nothing_moved(w: World, monkeypatch) -> None:
    """R2-04: leg 1 is written, then the audit log fails. The user must not be told
    nothing moved; reconcile reports the stuck transfer."""
    tid = w.propose("50").transfer_id
    _fail_when(monkeypatch, w.audit, "transition", lambda _t, status, **_: status is S.LEG1_DONE)
    reply = w.session.on_user_message("yes")
    assert reply.handled and "Nothing was moved" not in str(reply.reply)
    assert "check your balances" in str(reply.reply)
    assert w.audit.get(str(tid)).status is S.EXECUTING
    assert w.audit.safety_events()[-1].outcome == "EXECUTING"
    monkeypatch.undo()
    report = reconcile(w.bank, w.audit, [w.checking.id, w.savings.id], now=w.clock)
    assert report.count("stuck_in_progress") == 1


def test_if_the_executed_event_cannot_be_logged_the_reply_still_arrives(
    w: World, monkeypatch
) -> None:
    """V2-02: the money has moved; a failed log write must not swallow the reply (the
    user would think nothing happened and might ask again)."""
    tid = w.propose("50").transfer_id
    _fail_when(monkeypatch, w.audit, "log_event", lambda _c, kind, **_: kind == "executed")
    reply = w.session.on_user_message("yes")
    assert reply.handled and reply.reply and "50" in reply.reply
    assert w.audit.get(str(tid)).status is S.COMPLETED
    assert w.balances() == (950, 550, -CARD_OWED)


def test_if_the_confirmation_cannot_be_logged_nothing_moves(w: World, monkeypatch) -> None:
    """V2-03: no money moves unless the "yes" is on record."""
    tid = w.propose("50").transfer_id
    _fail_when(monkeypatch, w.audit, "log_event", lambda _c, kind, **_: kind == "confirmed")
    reply = w.session.on_user_message("yes")
    assert reply.handled and "Nothing was moved" in str(reply.reply)
    assert w.audit.get(str(tid)).status is S.CANCELLED
    assert w.balances() == (1000, 500, -CARD_OWED)
    assert w.bank.list_withdrawals(w.checking.id) == []


def test_if_a_retry_cannot_be_logged_nothing_is_left_pending(w: World, monkeypatch) -> None:
    """V2-03: the retry read-back must not be lost while the transfer stays pending
    (a later "yes" would then execute a read-back the user never heard)."""
    tid = w.propose("50").transfer_id
    _flaky_reads(w, monkeypatch, failures=1)
    _fail_when(monkeypatch, w.audit, "log_event", lambda _c, kind, **_: kind == "retry_offered")
    reply = w.session.on_user_message("yes")
    assert reply.handled and "Nothing was moved" in str(reply.reply)
    assert w.session.pending_transfer_id is None
    assert w.audit.get(str(tid)).status is S.CANCELLED
    assert w.session.on_user_message("yes") == Intercept(handled=False)
    assert w.balances() == (1000, 500, -CARD_OWED)


def test_a_card_payment_is_checked_again_at_execution(w: World) -> None:
    """V2-05: a payment that lands between proposal and "yes" lowers what the card
    owes; the pending payment must not overpay it."""
    tid = w.propose("200", dst=w.card).transfer_id
    w.bank.create_deposit(w.card.id, 250, TODAY, "payment from elsewhere")  # owes $50 now
    reply = w.session.on_user_message("yes")
    assert reply.handled and "only owes $50" in str(reply.reply)
    assert "Nothing was moved" in str(reply.reply)
    assert w.audit.get(str(tid)).status is S.CANCELLED
    assert w.balances() == (1000, 500, -50)


def test_a_cancel_that_finds_the_money_already_moved_does_not_say_nothing_moved(
    w: World,
) -> None:
    """R2-05: something else (another runtime, a bug) executed the transfer; the user's
    "no" must not produce "Nothing was moved"."""
    tid = str(w.propose("50").transfer_id)
    w.service.execute_pending(tid, customer_id=w.session.customer_id)
    reply = w.session.on_user_message("no thanks")
    assert not reply.handled and reply.note
    assert "Nothing was moved" not in reply.note and "COMPLETED" in reply.note
    kinds = [(e.kind, e.outcome) for e in w.audit.safety_events()]
    assert kinds[-2:] == [("state_mismatch", "COMPLETED"), ("cancelled_not_a_yes", "COMPLETED")]


def test_a_proposal_during_a_slow_execution_waits_for_it(w: World, monkeypatch) -> None:
    """R2-05: the session's methods are serialized. Without the lock, a proposal made
    while a retry was being decided could be overwritten and left PENDING forever."""
    tid = w.propose("50").transfer_id
    entered, release = threading.Event(), threading.Event()

    def slow_temporary_failure(transfer_id, *, customer_id):
        entered.set()
        assert release.wait(5)
        raise TransferRejected("bank busy", retryable=True)

    monkeypatch.setattr(w.service, "execute_pending", slow_temporary_failure)
    answer = threading.Thread(target=w.session.on_user_message, args=("yes",))
    answer.start()
    assert entered.wait(5)
    other = threading.Thread(target=w.propose, args=("20",))
    other.start()
    other.join(0.3)  # with the lock it is still waiting here
    release.set()
    answer.join(5)
    other.join(5)
    stranded = [
        r.transfer_id
        for r in w.audit.records()
        if r.status is S.PENDING and r.transfer_id != w.session.pending_transfer_id
    ]
    assert stranded == []
    assert w.audit.get(str(tid)).status is S.CANCELLED  # the 2nd proposal cancelled both
    assert w.session.pending_transfer_id is None


# --- the event log (D55, R2-08) -----------------------------------------------------------
def test_events_carry_the_session_and_the_real_outcome(w: World) -> None:
    w.propose("50")
    w.session.on_user_message("yes")
    events = w.audit.safety_events(session_id="s-test")
    assert [(e.kind, e.outcome) for e in events] == [
        ("proposed", "PENDING"),
        ("confirmed", "PENDING"),
        ("executed", "COMPLETED"),
    ]


def test_an_executed_event_says_how_execution_ended(w: World) -> None:
    """A compensated transfer is still logged as executed, with its real status."""
    w.propose("50")
    w.bank.fail_next("create_deposit")  # leg 2 rejected -> leg 1 returned
    reply = w.session.on_user_message("yes")
    assert reply.handled
    last = w.audit.safety_events()[-1]
    assert (last.kind, last.outcome) == ("executed", "COMPENSATED")
    assert w.balances() == (1000, 500, -CARD_OWED)


def test_two_sessions_for_one_customer_are_told_apart(w: World) -> None:
    """Eval runs share the seed's customers; the session id keeps their events apart."""
    other = SafetySession(
        w.bank, w.service, w.audit, w.session.customer_id, session_id="s-other", now=w.clock
    )
    w.propose("5")
    other.propose(w.checking.last4, w.savings.last4, "7")
    assert len(w.audit.safety_events(session_id="s-test")) == 1
    assert len(w.audit.safety_events(session_id="s-other")) == 1
    assert SafetySession(w.bank, w.service, w.audit, "c").session_id.startswith("sess-")


# --- the guarantee, at scale -----------------------------------------------------------
# Every generated message contains at least one of these. None of them can appear in a
# plain yes (checked below), so each message is known NOT to be a yes by construction;
# the classifier under test is never used to decide that (R2-07).
POISON_WORDS = (
    "but no not wait instead maybe later send more to savings ignore actually cancel "
    "never stop hold and then also twice again"
).split()
POISON_CHARS = [
    "?", "0", "5", "$", "-", ":", ";", '"', "\u200b", "\u200d", "\u00a0", "\u2028",
    "\u3000", "\x1c", "\x1f", "\x0b", "\ufeff", "\u202e", "\u0435", "\uff59",
]  # fmt: skip
HOMOGLYPHS = {"e": "\u0435", "o": "\u043e", "a": "\u0430", "i": "\u0456", "y": "\u0443"}
_WORD = re.compile("[a-z]+")


def _is_poisoned(msg: str) -> bool:
    """The independent oracle: True if the message holds a word or character that no
    plain yes can contain."""
    return any(c in msg for c in POISON_CHARS + list(HOMOGLYPHS.values())) or any(
        w in POISON_WORDS for w in _WORD.findall(msg.lower())
    )


def test_no_yes_phrase_contains_a_poison_word_or_character() -> None:
    """Keeps the oracle honest if YES_PHRASES ever changes."""
    for phrase in YES_PHRASES:
        assert not _is_poisoned(phrase), phrase


def _near_miss_messages(rng: random.Random, n: int) -> list[str]:
    """Messages built to look like a yes without being one."""
    phrases = sorted(YES_PHRASES) + ["yes"] * 10
    seps = [" ", ", ", "  ", "\n", ""]
    out: list[str] = []
    while len(out) < n:
        phrase = rng.choice(phrases)
        if rng.random() < 0.5:
            phrase = rng.choice([phrase, phrase.upper(), phrase.capitalize()])
        roll = rng.random()
        if roll < 0.3:  # the "yes, but ..." family, which a prefix check would accept
            tail = " ".join(rng.sample(POISON_WORDS, rng.randint(1, 3)))
            msg = phrase + rng.choice(seps[:4]) + tail + rng.choice(["", ".", "!", " 500"])
        elif roll < 0.45:
            msg = rng.choice(POISON_WORDS) + rng.choice(seps[:4]) + phrase
        elif roll < 0.75:  # one odd character anywhere, including at either end
            i = rng.randrange(len(phrase) + 1)
            msg = phrase[:i] + rng.choice(POISON_CHARS) + phrase[i:]
        elif roll < 0.9:  # a look-alike letter
            letters = [i for i, c in enumerate(phrase.lower()) if c in HOMOGLYPHS]
            if not letters:
                continue
            i = rng.choice(letters)
            msg = phrase[:i] + HOMOGLYPHS[phrase[i].lower()] + phrase[i + 1 :]
        else:  # noise that still holds a poison character
            noise = "".join(rng.choice("yesnoak .,!") for _ in range(rng.randint(0, 8)))
            msg = noise + rng.choice(POISON_CHARS)
        assert _is_poisoned(msg), repr(msg)  # the generator can't produce a real yes
        out.append(msg)
    return out


def test_the_classifier_rejects_every_near_miss() -> None:
    """R2-07: asserts on the classifier itself. A "starts with yes" checker, or one
    that folds Unicode spaces away, fails here."""
    messages = _near_miss_messages(random.Random(20261127), 5000)
    assert len(set(messages)) > 2000  # genuinely varied
    accepted = [m for m in messages if is_clear_yes(m)]
    assert accepted == []


def test_no_near_miss_reply_ever_moves_money() -> None:
    """DoD: thousands of near-miss replies through the session; every one cancels,
    none executes, and reconcile is clean."""
    w = make_world()
    messages = _near_miss_messages(random.Random(20261124), 3000)
    for msg in messages:
        tid = w.propose("5").transfer_id
        reply = w.session.on_user_message(msg)
        assert not reply.handled, repr(msg)
        assert w.audit.get(str(tid)).status is S.CANCELLED, repr(msg)
    assert w.balances() == (1000, 500, -CARD_OWED)
    assert all(r.status is S.CANCELLED for r in w.audit.records())
    assert w.bank.list_withdrawals(w.checking.id) == []
    report = reconcile(w.bank, w.audit, [w.checking.id, w.savings.id, w.card.id])
    assert report.ok


def test_mixed_conversation_reconciles_cleanly(w: World, monkeypatch) -> None:
    """Every kind of outcome in one conversation; reconcile finds no unconfirmed money."""
    w.propose("50")
    w.session.on_user_message("yes")  # completed
    w.propose("20")
    w.session.on_user_message("no")  # cancelled
    w.propose("30")
    w.clock.t += timedelta(minutes=3)
    w.session.on_user_message("yes")  # expired
    w.propose("10", dst=w.card)
    w.session.on_user_message("go ahead")  # card payment
    assert w.balances() == (940, 550, -CARD_OWED + 10)
    accounts = [w.checking.id, w.savings.id, w.card.id]
    assert reconcile(w.bank, w.audit, accounts).ok
    executed = [e for e in w.audit.safety_events() if e.kind == Event.EXECUTED]
    confirmed = [e for e in w.audit.safety_events() if e.kind == Event.CONFIRMED]
    expired = [e for e in w.audit.safety_events() if e.kind == Event.EXPIRED]
    # the late "yes" never counts as a confirmation: expiry is checked first
    assert (len(executed), len(confirmed), len(expired)) == (2, 2, 1)
