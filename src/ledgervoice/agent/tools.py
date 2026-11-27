"""The agent's four tools (D25): what the model can see and do.

Every number the model reports comes from here: totals and counts are computed in
code, never by the model. The tools read the bank live on each call (no snapshot), so
a transfer made a moment ago is reflected in the next answer.

- get_accounts: type, nickname, last 4 digits and computed balance (D18); a credit
  card is reported as the amount owed, a positive number (D45).
- get_transactions: spending (completed purchases, D48) or income (completed deposits
  into checking, excluding the app's own transfer legs) in an inclusive date range,
  optionally filtered by category, merchant or account; returns total, count, a
  per-category breakdown, pending items separately and the 20 most recent items.
  A purchase whose merchant can't be found is reported under "unknown", never
  dropped (D47). Merchant names match case-insensitively by substring; if nothing
  matches, the 3 closest names are returned so the model can ask (D60).
- get_bills: the customer's bills, optionally by status.
- propose_transfer: hands the request to the SafetySession (D53). It can only
  propose; confirmation belongs to the runtime (D26).

Tool input comes from the model, so it is validated here; problems come back as an
error result the model can explain, never as an exception.
"""

from __future__ import annotations

import difflib
import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from nessie_tools import (
    Account,
    AccountType,
    BankBackend,
    BankError,
    BillStatus,
    NotFoundError,
    Purchase,
    TxStatus,
    compute_balance,
)
from nessie_tools.transfers import TRANSFER_ID_PREFIX

from ledgervoice.safety import SafetySession
from ledgervoice.safety.policy import describe
from ledgervoice.seed.catalog import CATEGORIES

MAX_ITEMS = 20
UNKNOWN = "unknown"  # category and merchant name for a merchant that can't be found
KINDS = ("spending", "income")

_LAST4_HELP = "The last 4 digits of the account, exactly as get_accounts reports them."

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_accounts",
        "description": (
            "List the customer's accounts with their current balances. Each account has "
            "a type (checking, savings, credit card), a nickname and its last 4 digits. "
            "A credit card is reported as amount_owed (a positive number), not a balance. "
            "Call this before answering any question about balances, and to learn the "
            "last 4 digits needed by the other tools."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_transactions",
        "description": (
            "Total and list the customer's spending or income between two dates "
            "(inclusive). Spending = completed purchases on any account; income = "
            "completed deposits into checking. Moves between the customer's own "
            "accounts (savings transfers, card payments) are neither. Use the total and "
            "count returned here; never add up items yourself, because only the 20 most "
            "recent items are listed. Pending purchases are reported separately and are "
            "not part of the total. If a merchant name matches nothing, the closest "
            "names are returned so you can ask the customer which one they meant."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "First day, YYYY-MM-DD."},
                "end": {"type": "string", "description": "Last day, YYYY-MM-DD (inclusive)."},
                "kind": {
                    "type": "string",
                    "enum": list(KINDS),
                    "description": "spending (default) or income.",
                },
                "category": {
                    "type": "string",
                    "enum": [*CATEGORIES, UNKNOWN],
                    "description": "Only spending in this category.",
                },
                "merchant": {
                    "type": "string",
                    "description": "Only purchases at merchants whose name contains this.",
                },
                "account": {"type": "string", "description": _LAST4_HELP},
            },
            "required": ["start", "end"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_bills",
        "description": (
            "List the customer's bills: payee, amount, status, the day of the month it "
            "recurs and the next payment date, and the account it is paid from."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": [s.value for s in BillStatus]},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_transfer",
        "description": (
            "Propose moving money between two of the customer's own accounts. This does "
            "NOT move money: the app reads the exact transfer back to the customer and "
            "only their own clear yes executes it. Propose one transfer at a time, and "
            "only when the customer has asked for it. Whole dollars only, at most $2,000. "
            "Money can go into a credit card (a payment, at most what it owes) but never "
            "out of one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "from_last4": {"type": "string", "description": _LAST4_HELP},
                "to_last4": {"type": "string", "description": _LAST4_HELP},
                "amount": {
                    "type": "string",
                    "description": 'The amount in dollars as digits, e.g. "50" or "1,200".',
                },
            },
            "required": ["from_last4", "to_last4", "amount"],
            "additionalProperties": False,
        },
    },
]

TOOL_NAMES = frozenset(t["name"] for t in TOOL_SCHEMAS)


@dataclass(frozen=True)
class ToolOutcome:
    """`content` goes back to the model as the tool result. A successful proposal
    carries the code-written `read_back`; the engine shows it and ends the turn."""

    content: str
    is_error: bool = False
    read_back: str | None = None
    transfer_id: str | None = None


class InvalidInput(ValueError):
    """Bad tool input from the model; the message is returned to it as an error."""


class BankTools:
    """The tools for one customer's conversation. Merchant names are cached for the
    session (they never change); everything else is read live."""

    def __init__(self, backend: BankBackend, customer_id: str, safety: SafetySession) -> None:
        self._bank = backend
        self._customer_id = customer_id
        self._safety = safety
        self._merchants: dict[str, tuple[str, str]] = {}  # id -> (name, category)
        self._handlers: dict[str, Callable[[dict[str, Any]], ToolOutcome]] = {
            "get_accounts": self._get_accounts,
            "get_transactions": self._get_transactions,
            "get_bills": self._get_bills,
            "propose_transfer": self._propose_transfer,
        }

    def run(self, name: str, tool_input: object) -> ToolOutcome:
        handler = self._handlers.get(name)
        if handler is None:
            return _error(f"there is no tool called {name!r}")
        if not isinstance(tool_input, dict):
            return _error("the tool input must be an object")
        try:
            return handler(tool_input)
        except InvalidInput as exc:
            return _error(str(exc))
        except BankError:
            return _error(
                "the bank could not be reached just now; tell the customer and suggest "
                "trying again in a moment"
            )

    # --- get_accounts ------------------------------------------------------------
    def _get_accounts(self, tool_input: dict[str, Any]) -> ToolOutcome:
        _no_extra(tool_input, set())
        rows = []
        for account in self._accounts():
            current = compute_balance(self._bank, account.id, account=account).current
            row: dict[str, Any] = {
                "type": _type_word(account),
                "nickname": account.nickname,
                "last4": account.last4,
            }
            if account.type is AccountType.CREDIT_CARD:
                row["amount_owed"] = -current  # D45
            else:
                row["balance"] = current
            rows.append(row)
        return _ok({"accounts": rows})

    # --- get_transactions --------------------------------------------------------
    def _get_transactions(self, tool_input: dict[str, Any]) -> ToolOutcome:
        allowed = {"start", "end", "kind", "category", "merchant", "account"}
        _no_extra(tool_input, allowed)
        start = _date(tool_input.get("start"), "start")
        end = _date(tool_input.get("end"), "end")
        if start > end:
            raise InvalidInput("start must be on or before end")
        kind = _optional_text(tool_input, "kind") or "spending"
        if kind not in KINDS:
            raise InvalidInput(f"kind must be one of {', '.join(KINDS)}")
        category = _optional_text(tool_input, "category")
        if category is not None and category not in (*CATEGORIES, UNKNOWN):
            raise InvalidInput(
                f"unknown category {category!r}; use one of {', '.join([*CATEGORIES, UNKNOWN])}"
            )
        merchant = _optional_text(tool_input, "merchant")
        accounts = self._accounts()
        account_ref = _optional_text(tool_input, "account")
        if account_ref is not None:
            accounts = [a for a in accounts if a.last4 == account_ref.strip()]
            if not accounts:
                raise InvalidInput(f"the customer has no account ending in {account_ref}")
        if kind == "income":
            if category is not None or merchant is not None:
                raise InvalidInput("category and merchant only apply to spending")
            return self._income(accounts, start, end)
        return self._spending(accounts, start, end, category, merchant)

    def _spending(
        self,
        accounts: list[Account],
        start: date,
        end: date,
        category: str | None,
        merchant: str | None,
    ) -> ToolOutcome:
        purchases = [(a, p) for a in accounts for p in self._bank.list_purchases(a.id)]
        matched: list[str] | None = None
        if merchant is not None:
            names = sorted({self._merchant(p.merchant_id)[0] for _, p in purchases} - {UNKNOWN})
            needle = merchant.strip().casefold()
            matched = [n for n in names if needle and needle in n.casefold()]
            if not matched:
                closest = difflib.get_close_matches(merchant, names, n=3, cutoff=0.0)
                return _ok(
                    {
                        "matched_merchants": [],
                        "closest_merchants": closest,
                        "note": f"no merchant name contains {merchant!r}",
                    }
                )
        completed: list[tuple[Account, Purchase]] = []
        pending: list[tuple[Account, Purchase]] = []
        for account, p in purchases:
            day = p.purchase_date
            if day is None or not start <= day <= end:
                continue
            name, cat = self._merchant(p.merchant_id)
            if matched is not None and name not in matched:
                continue
            if category is not None and cat != category:
                continue
            if p.status is TxStatus.COMPLETED:
                completed.append((account, p))
            elif p.status is TxStatus.PENDING:
                pending.append((account, p))
        by_category: dict[str, int] = defaultdict(int)
        for _, p in completed:
            by_category[self._merchant(p.merchant_id)[1]] += p.amount
        result: dict[str, Any] = {
            "kind": "spending",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "total": sum(p.amount for _, p in completed),
            "count": len(completed),
            "by_category": dict(sorted(by_category.items(), key=lambda kv: (-kv[1], kv[0]))),
            "pending": {
                "total": sum(p.amount for _, p in pending),
                "count": len(pending),
            },
            "items": self._purchase_items(completed),
            "items_shown": min(len(completed), MAX_ITEMS),
        }
        if matched is not None:
            result["matched_merchants"] = matched
        return _ok(result)

    def _purchase_items(self, rows: list[tuple[Account, Purchase]]) -> list[dict[str, Any]]:
        latest = sorted(rows, key=lambda r: (r[1].purchase_date or date.min, r[1].id), reverse=True)
        items = []
        for account, p in latest[:MAX_ITEMS]:
            name, cat = self._merchant(p.merchant_id)
            items.append(
                {
                    "date": p.purchase_date.isoformat() if p.purchase_date else None,
                    "merchant": name,
                    "category": cat,
                    "amount": p.amount,
                    "account": describe(account),
                }
            )
        return items

    def _income(self, accounts: list[Account], start: date, end: date) -> ToolOutcome:
        rows = []
        for account in accounts:
            if account.type is not AccountType.CHECKING:
                continue
            for d in self._bank.list_deposits(account.id):
                if d.status is not TxStatus.COMPLETED or d.transaction_date is None:
                    continue
                if TRANSFER_ID_PREFIX in d.description:  # the app's own transfer legs
                    continue
                if start <= d.transaction_date <= end:
                    rows.append((account, d))
        rows.sort(key=lambda r: (r[1].transaction_date or date.min, r[1].id), reverse=True)
        return _ok(
            {
                "kind": "income",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "total": sum(d.amount for _, d in rows),
                "count": len(rows),
                "items": [
                    {
                        "date": d.transaction_date.isoformat() if d.transaction_date else None,
                        "description": d.description,
                        "amount": d.amount,
                        "account": describe(a),
                    }
                    for a, d in rows[:MAX_ITEMS]
                ],
                "items_shown": min(len(rows), MAX_ITEMS),
            }
        )

    # --- get_bills ---------------------------------------------------------------
    def _get_bills(self, tool_input: dict[str, Any]) -> ToolOutcome:
        _no_extra(tool_input, {"status"})
        status_text = _optional_text(tool_input, "status")
        status: BillStatus | None = None
        if status_text is not None:
            try:
                status = BillStatus(status_text)
            except ValueError:
                raise InvalidInput(
                    f"status must be one of {', '.join(s.value for s in BillStatus)}"
                ) from None
        rows = []
        for account in self._accounts():
            for b in self._bank.list_bills(account.id):
                if status is not None and b.status is not status:
                    continue
                rows.append(
                    {
                        "payee": b.payee,
                        "nickname": b.nickname,
                        "amount": b.payment_amount,
                        "status": b.status.value,
                        "recurring_day": b.recurring_date,
                        "next_payment_date": b.payment_date.isoformat() if b.payment_date else None,
                        "account": describe(account),
                    }
                )
        rows.sort(key=lambda r: (r["next_payment_date"] or "", r["payee"]))
        return _ok({"bills": rows, "count": len(rows)})

    # --- propose_transfer --------------------------------------------------------
    def _propose_transfer(self, tool_input: dict[str, Any]) -> ToolOutcome:
        _no_extra(tool_input, {"from_last4", "to_last4", "amount"})
        try:
            result = self._safety.propose(
                tool_input.get("from_last4"), tool_input.get("to_last4"), tool_input.get("amount")
            )
        except Exception:  # e.g. the audit log failed; the session left nothing pending
            return _error(
                "the transfer could not be set up because of an internal problem; nothing "
                "is waiting for confirmation and nothing was moved. Tell the customer."
            )
        if not result.ok:
            return _error(result.say)
        return ToolOutcome(
            content=(
                "The app has shown the customer this exact read-back and is waiting for "
                f"their answer: {result.say!r}. Their next message is that answer; the "
                "app handles it. Do not ask for confirmation yourself."
            ),
            read_back=result.say,
            transfer_id=result.transfer_id,
        )

    # --- helpers -----------------------------------------------------------------
    def _accounts(self) -> list[Account]:
        return sorted(self._bank.list_accounts(self._customer_id), key=lambda a: (a.type, a.id))

    def _merchant(self, merchant_id: str) -> tuple[str, str]:
        cached = self._merchants.get(merchant_id)
        if cached is None:
            try:
                m = self._bank.get_merchant(merchant_id)
                cached = (m.name or UNKNOWN, m.category if isinstance(m.category, str) else UNKNOWN)
            except NotFoundError:  # D47: Nessie accepts purchases at unknown merchants
                cached = (UNKNOWN, UNKNOWN)
            self._merchants[merchant_id] = cached
        return cached


def _type_word(account: Account) -> str:
    return {
        AccountType.CHECKING: "checking",
        AccountType.SAVINGS: "savings",
        AccountType.CREDIT_CARD: "credit card",
    }.get(account.type, "account")


def _ok(payload: dict[str, Any]) -> ToolOutcome:
    return ToolOutcome(json.dumps(payload, separators=(",", ":")))


def _error(message: str) -> ToolOutcome:
    return ToolOutcome(message, is_error=True)


def _no_extra(tool_input: dict[str, Any], allowed: set[str]) -> None:
    extra = sorted(set(tool_input) - allowed)
    if extra:
        raise InvalidInput(f"unexpected input: {', '.join(extra)}")


def _optional_text(tool_input: dict[str, Any], key: str) -> str | None:
    value = tool_input.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidInput(f"{key} must be text")
    return value


def _date(value: object, key: str) -> date:
    if not isinstance(value, str):
        raise InvalidInput(f"{key} must be a date like 2026-11-24")
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise InvalidInput(f"{key} must be a date like 2026-11-24, not {value!r}") from None
