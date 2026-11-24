"""M1d: the conversation safety layer. Money moves only after a clear, recent yes."""

from __future__ import annotations

import random
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
    ],
)
def test_amounts_that_parse(text: str, dollars: int) -> None:
    assert parse_amount(text) == dollars


@pytest.mark.parametrize(
    ("text", "says"),
    [
        ("50.5", "$50 or $51"),
        ("$50.99", "$50 or $51"),
        ("0.5", "$1 instead"),
        ("0", "more than zero"),
        ("0.00", "more than zero"),
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
    ],
)
def test_amounts_that_are_refused(text: str, says: str) -> None:
    with pytest.raises(AmountRejected, match=says.replace("$", r"\$")):
        parse_amount(text)


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
    ],
)
def test_adversarial_replies_are_not_a_yes(text: str) -> None:
    assert not is_clear_yes(text)


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


def make_world(*, session_max_age=timedelta(minutes=2), service_max_age=timedelta(minutes=2)):
    clock = Clock()
    bank = FakeBackend(seed=11)
    me = bank.create_customer("Pat", "Safe", ADDRESS)
    them = bank.create_customer("Sam", "Other", ADDRESS)
    checking = bank.create_account(me.id, AccountType.CHECKING, "Checking", 1000)
    savings = bank.create_account(me.id, AccountType.SAVINGS, "Savings", 500)
    card = bank.create_account(me.id, AccountType.CREDIT_CARD, "Card", 0)
    stranger = bank.create_account(them.id, AccountType.CHECKING, "Theirs", 900)
    audit = AuditLog(":memory:", now=clock)
    service = TransferService(
        bank, audit, today=lambda: TODAY, sleep=lambda _: None, now=clock, max_age=service_max_age
    )
    session = SafetySession(bank, service, audit, me.id, now=clock, max_age=session_max_age)
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
    result = w.propose("200", dst=w.card)
    assert result.ok and "to credit card ending in" in result.say
    assert w.session.on_user_message("yes").handled
    assert w.balances() == (800, 500, 200)


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
    assert w.balances() == (1000, 500, 0)  # proposing moves nothing
    reply = w.session.on_user_message("Yes, please!")
    assert reply.handled and reply.reply and "50" in reply.reply
    assert w.balances() == (950, 550, 0)
    assert w.audit.get(result.transfer_id).status is S.COMPLETED
    assert w.events() == ["proposed", "confirmed", "executed"]
    assert w.session.pending_transfer_id is None


def test_anything_but_yes_cancels_and_goes_to_the_model(w: World) -> None:
    tid = w.propose("50").transfer_id
    reply = w.session.on_user_message("hmm, make it 40 instead")
    assert not reply.handled and reply.note and "cancelled" in reply.note
    assert w.audit.get(str(tid)).status is S.CANCELLED
    assert w.balances() == (1000, 500, 0)
    assert w.events() == ["proposed", "cancelled_not_a_yes"]


def test_a_late_yes_after_a_no_does_nothing(w: World) -> None:
    """Expiry after one user turn: the pending transfer is gone after any reply."""
    w.propose("50")
    w.session.on_user_message("wait")
    assert w.session.on_user_message("yes") == Intercept(handled=False)
    assert w.balances() == (1000, 500, 0)


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
    assert w.balances() == (1000, 500, 0)
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
    assert w.balances() == (1000, 500, 0)


# --- replacing and refusing ---------------------------------------------------------------
def test_a_new_proposal_replaces_the_pending_one(w: World) -> None:
    first = w.propose("50").transfer_id
    second = w.propose("70").transfer_id
    assert first != second and w.session.pending_transfer_id == second
    assert w.audit.get(str(first)).status is S.CANCELLED
    assert w.session.on_user_message("yes").handled
    assert w.balances() == (930, 570, 0)  # only the second one ran
    assert w.events() == ["proposed", "replaced", "proposed", "confirmed", "executed"]


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


def test_a_refused_new_proposal_still_keeps_the_old_one_pending(w: World) -> None:
    """A bad follow-up request must not silently cancel a good pending one; but the
    user's next message settles the old one as usual."""
    tid = w.propose("50").transfer_id
    assert not w.propose("abc").ok
    assert w.session.pending_transfer_id == tid


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
    assert w.balances() == (950, 550, 0)
    assert w.events() == ["proposed", "confirmed", "retry_offered", "confirmed", "executed"]


def test_a_retry_needs_a_real_yes_too(w: World, monkeypatch) -> None:
    tid = w.propose("50").transfer_id
    _flaky_reads(w, monkeypatch, failures=1)
    w.session.on_user_message("yes")
    assert not w.session.on_user_message("never mind").handled
    assert w.audit.get(str(tid)).status is S.CANCELLED
    assert w.balances() == (1000, 500, 0)


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


# --- the guarantee, at scale -----------------------------------------------------------
def _twisted_messages(rng: random.Random, n: int) -> list[str]:
    """Messages built to look like a yes without being one, plus random noise."""
    base = sorted(YES_PHRASES) + ["yes"] * 10
    extras = [
        "but",
        "and",
        "no",
        "wait",
        "?",
        "send 500",
        "to savings",
        "instead",
        "maybe",
        "not",
        "ignore that",
        "$40",
        "later",
        "x",
        "​",
        "е",
        "please?",
    ]
    out: list[str] = []
    while len(out) < n:
        phrase = rng.choice(base)
        roll = rng.random()
        if roll < 0.25:
            msg = f"{phrase} {rng.choice(extras)}"
        elif roll < 0.45:
            msg = f"{rng.choice(extras)} {phrase}"
        elif roll < 0.6:
            i = rng.randrange(len(phrase) + 1)
            msg = phrase[:i] + rng.choice("abcxyz?0 ​") + phrase[i:]
        elif roll < 0.7:
            msg = phrase + "?"
        elif roll < 0.8:
            msg = phrase.replace("e", "е")
        elif roll < 0.9:
            msg = "".join(rng.choice("yesnoak ?!.,") for _ in range(rng.randint(0, 12)))
        else:
            msg = phrase[: max(1, len(phrase) - 1)]
        if not is_clear_yes(msg):
            out.append(msg)
    return out


def test_no_message_that_is_not_a_clear_yes_ever_moves_money() -> None:
    """DoD: thousands of near-miss replies; every one cancels, none executes."""
    w = make_world()
    rng = random.Random(20261124)
    messages = _twisted_messages(rng, 3000)
    assert len(set(messages)) > 1000  # genuinely varied
    for msg in messages:
        tid = w.propose("5").transfer_id
        reply = w.session.on_user_message(msg)
        assert not reply.handled, msg
        assert w.audit.get(str(tid)).status is S.CANCELLED, msg
    assert w.balances() == (1000, 500, 0)
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
    assert w.balances() == (940, 550, 10)
    accounts = [w.checking.id, w.savings.id, w.card.id]
    assert reconcile(w.bank, w.audit, accounts).ok
    executed = [e for e in w.audit.safety_events() if e.kind == Event.EXECUTED]
    confirmed = [e for e in w.audit.safety_events() if e.kind == Event.CONFIRMED]
    expired = [e for e in w.audit.safety_events() if e.kind == Event.EXPIRED]
    # the late "yes" never counts as a confirmation: expiry is checked first
    assert (len(executed), len(confirmed), len(expired)) == (2, 2, 1)
