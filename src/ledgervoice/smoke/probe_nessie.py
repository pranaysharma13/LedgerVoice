"""Probe v2: verifies the Nessie behaviors M1 depends on, using the schema the server
itself reported on 2026-11-18 (the public docs are incomplete and partly wrong).

WRITES to your Nessie sandbox: one customer, two accounts, a few transactions. The two
accounts are deleted at the end unless you pass --keep. The probe merchant is reused
across runs (merchants can't be deleted). It never calls DELETE /data.

Questions:
  Q1  Is a back-dated purchase_date stored as sent?
  Q2  Is merchant category stored as a string?
  Q3  Do purchases / withdrawals / deposits / native transfers change balances?
  Q4  Is a non-integer amount rejected, or silently truncated?            (D16)
  Q5  Can a withdrawal be cancelled via PUT, and does that restore the balance?
      (the compensating action for the transfer saga, D15)
  Q6  Does PUT /accounts/{id} let us set a balance?   (fallback if Q3 says "no change")
  Q7  What does an empty transfers list return?
  Q8  Does DELETE /accounts/{id} work?                (cleanup between eval runs)

Run:  python -m ledgervoice.smoke.probe_nessie --yes [--keep]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, timedelta
from typing import Any

from nessie_tools import NessieClient, NessieError

from ledgervoice.config import Settings, get_settings

ADDRESS = {
    "street_number": "100",
    "street_name": "Probe St",
    "city": "Arlington",
    "state": "VA",
    "zip": "22201",
}
MERCHANT_NAME = "LV Probe Coffee"
SETTLE_SECONDS = 3  # re-read balances after a pause, in case updates are asynchronous


class Probe:
    def __init__(self, client: NessieClient) -> None:
        self.c = client
        self.log: list[dict[str, Any]] = []

    def call(
        self, label: str, method: str, path: str, body: Any = None, quiet: bool = False
    ) -> dict[str, Any]:
        """Send one request; record and return {ok, response | error, status_code}."""
        entry: dict[str, Any] = {"step": label, "request": f"{method} {path}", "body": body}
        try:
            entry["response"] = self.c.request(method, path, json=body)
            entry["ok"] = True
        except NessieError as exc:  # message is already key-redacted
            entry.update(ok=False, error=str(exc), status_code=exc.status_code)
        self.log.append(entry)
        if not quiet or not entry["ok"]:
            print(f"  [{'ok ' if entry['ok'] else 'ERR'}] {label}")
        if not entry["ok"]:
            print(f"        {entry['error'][:400]}")
        return entry

    def balance(self, account_id: str) -> Any:
        e = self.call("read balance", "GET", f"/accounts/{account_id}", quiet=True)
        r = e.get("response")
        return r.get("balance") if isinstance(r, dict) else None

    def find(self, path: str, description: str) -> dict[str, Any] | None:
        """Find a transaction on an account list endpoint by its description."""
        items = self.call(f"list {path}", "GET", path, quiet=True).get("response") or []
        if not isinstance(items, list):
            return None
        return next(
            (x for x in items if isinstance(x, dict) and x.get("description") == description),
            None,
        )


def obj_id(resp: Any) -> str | None:
    """Create responses look like {"code": 201, "objectCreated": {"_id": ...}}."""
    if isinstance(resp, dict):
        obj = resp.get("objectCreated") or resp
        if isinstance(obj, dict):
            return obj.get("_id") or obj.get("id")
    return None


def delta(before: Any, after: Any) -> Any:
    if isinstance(before, int | float) and isinstance(after, int | float):
        return after - before
    return None


def find_or_create_merchant(p: Probe, f: dict[str, Any]) -> str | None:
    merchants = p.call("list merchants", "GET", "/merchants", quiet=True).get("response") or []
    existing = [m for m in merchants if isinstance(m, dict) and m.get("name") == MERCHANT_NAME]
    if existing:
        mid = existing[0].get("_id")
        print(f"  [ok ] reusing merchant {MERCHANT_NAME!r}")
    else:
        body = {
            "name": MERCHANT_NAME,
            "category": "food",
            "address": ADDRESS,
            "geocode": {"lat": 38.88, "lng": -77.10},
        }
        mid = obj_id(p.call("create merchant", "POST", "/merchants", body).get("response"))
    if mid:
        m = p.call("read merchant", "GET", f"/merchants/{mid}", quiet=True).get("response")
        f["Q2_merchant_category_stored"] = m.get("category") if isinstance(m, dict) else None
    return mid


def run_questions(
    p: Probe, f: dict[str, Any], chk: str, sav: str, merchant: str | None, past: str, today: str
) -> None:
    purchases = f"/accounts/{chk}/purchases"

    if merchant:
        print("Q1/Q3: back-dated purchase ($5, 10 days ago)")
        b0 = p.balance(chk)
        p.call(
            "create purchase",
            "POST",
            purchases,
            {
                "merchant_id": merchant,
                "medium": "balance",
                "amount": 5,
                "purchase_date": past,
                "description": "LV probe latte",
                "status": "completed",
            },
        )
        f["Q3_purchase_checking_delta"] = delta(b0, p.balance(chk))
        latte = p.find(purchases, "LV probe latte")
        f["Q1_purchase_date_sent"] = past
        f["Q1_purchase_date_stored"] = latte.get("purchase_date") if latte else None
        f["purchase_fields"] = sorted(latte) if latte else None

        print("Q4: non-integer amount ($4.75)")
        b0 = p.balance(chk)
        e = p.call(
            "create purchase with amount 4.75",
            "POST",
            purchases,
            {
                "merchant_id": merchant,
                "medium": "balance",
                "amount": 4.75,
                "purchase_date": today,
                "description": "LV probe fraction",
                "status": "completed",
            },
        )
        if e["ok"]:
            frac = p.find(purchases, "LV probe fraction")
            stored = frac.get("amount") if frac else "?"
            f["Q4_non_integer_amount"] = f"ACCEPTED, stored amount = {stored}"
        else:
            f["Q4_non_integer_amount"] = f"REJECTED (HTTP {e.get('status_code')})"
        f["Q4_checking_delta"] = delta(b0, p.balance(chk))
    else:
        print("Skipping purchase questions: no merchant")

    print("Q3/Q5: transfer as withdrawal + deposit ($50, D15)")
    c0, s0 = p.balance(chk), p.balance(sav)
    w = p.call(
        "withdraw $50 from checking",
        "POST",
        f"/accounts/{chk}/withdrawals",
        {
            "medium": "balance",
            "amount": 50,
            "transaction_date": today,
            "description": "LV-TRF-probe leg 1/2",
            "status": "completed",
        },
    )
    f["Q3_withdrawal_checking_delta"] = delta(c0, p.balance(chk))
    p.call(
        "deposit $50 into savings",
        "POST",
        f"/accounts/{sav}/deposits",
        {
            "medium": "balance",
            "amount": 50,
            "transaction_date": today,
            "description": "LV-TRF-probe leg 2/2",
            "status": "completed",
        },
    )
    f["Q3_deposit_savings_delta"] = delta(s0, p.balance(sav))

    wid = obj_id(w.get("response"))
    if wid:
        c1 = p.balance(chk)
        p.call(
            "cancel the withdrawal via PUT", "PUT", f"/withdrawals/{wid}", {"status": "cancelled"}
        )
        f["Q5_cancel_checking_delta"] = delta(c1, p.balance(chk))
        wr = p.call("re-read withdrawal", "GET", f"/withdrawals/{wid}", quiet=True).get("response")
        f["Q5_withdrawal_status_after"] = wr.get("status") if isinstance(wr, dict) else None
    else:
        f["Q5_cancel_checking_delta"] = "skipped: withdrawal id not returned"

    print("Q7: empty transfers list")
    e = p.call("list transfers on savings", "GET", f"/accounts/{sav}/transfers", quiet=True)
    f["Q7_empty_transfers"] = (
        f"HTTP 200: {e.get('response')!r}"
        if e["ok"]
        else f"HTTP {e.get('status_code')}: {e['error'][-60:]}"
    )

    print("Q3: native transfer ($10; it has no destination field)")
    c0 = p.balance(chk)
    p.call(
        "create native transfer",
        "POST",
        f"/accounts/{chk}/transfers",
        {
            "transaction_date": today,
            "status": "completed",
            "amount": 10,
            "description": "LV probe native transfer",
        },
    )
    f["Q3_native_transfer_checking_delta"] = delta(c0, p.balance(chk))

    print(f"Waiting {SETTLE_SECONDS} s, then re-reading balances (async updates?)")
    time.sleep(SETTLE_SECONDS)
    f["final_checking_after_wait (started 1000)"] = p.balance(chk)
    f["final_savings_after_wait (started 500)"] = p.balance(sav)

    print("Q6: set a balance via PUT")
    p.call("PUT savings balance = 777", "PUT", f"/accounts/{sav}", {"balance": 777})
    f["Q6_savings_after_put (expect 777)"] = p.balance(sav)


def save(s: Settings, findings: dict[str, Any], log: list[dict[str, Any]]) -> None:
    out = s.artifacts_dir / "nessie_probe_v2.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    text = s.scrub(json.dumps({"findings": findings, "log": log}, indent=2, default=str))
    out.write_text(text, encoding="utf-8")
    print("\nFindings:")
    for k, v in findings.items():
        print(f"  {k}: {v}")
    print(f"\nFull request/response log: {out}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Nessie sandbox behavior (writes data)")
    parser.add_argument("--yes", action="store_true", help="confirm writing probe data")
    parser.add_argument("--keep", action="store_true", help="don't delete the probe accounts")
    args = parser.parse_args()
    if not args.yes:
        print("This writes test data to your Nessie sandbox. Re-run with --yes to proceed.")
        return 2

    logging.getLogger("httpx").setLevel(logging.WARNING)
    s = get_settings()
    s.require("nessie_api_key")
    assert s.nessie_api_key is not None
    findings: dict[str, Any] = {}
    past = (date.today() - timedelta(days=10)).isoformat()
    today = date.today().isoformat()

    with NessieClient(s.nessie_api_key.get_secret_value(), base_url=s.nessie_base_url) as client:
        p = Probe(client)
        print("Setup")
        cust = obj_id(
            p.call(
                "create customer",
                "POST",
                "/customers",
                {"first_name": "Pat", "last_name": "ProbeTwo", "address": ADDRESS},
            ).get("response")
        )
        if not cust:
            save(s, findings, p.log)
            return 1
        accounts = f"/customers/{cust}/accounts"
        chk = obj_id(
            p.call(
                "create checking ($1000)",
                "POST",
                accounts,
                {"type": "Checking", "nickname": "Probe2 Checking", "rewards": 0, "balance": 1000},
            ).get("response")
        )
        sav = obj_id(
            p.call(
                "create savings ($500)",
                "POST",
                accounts,
                {"type": "Savings", "nickname": "Probe2 Savings", "rewards": 0, "balance": 500},
            ).get("response")
        )
        if not (chk and sav):
            save(s, findings, p.log)
            return 1
        merchant = find_or_create_merchant(p, findings)

        try:
            run_questions(p, findings, chk, sav, merchant, past, today)
        finally:
            if args.keep:
                print("Keeping probe accounts (--keep)")
            else:
                print("Q8: cleanup")
                for label, acct in (("checking", chk), ("savings", sav)):
                    e = p.call(f"delete probe {label}", "DELETE", f"/accounts/{acct}")
                    findings[f"Q8_delete_{label}"] = (
                        "ok" if e["ok"] else f"HTTP {e.get('status_code')}"
                    )

    save(s, findings, p.log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
