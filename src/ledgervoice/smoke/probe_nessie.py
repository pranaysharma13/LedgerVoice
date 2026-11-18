"""Exploratory probe: answers the Nessie unknowns M1's design depends on.

WRITES to your Nessie sandbox (creates one customer, two accounts, one merchant, one
purchase, one transfer, one bill, all tagged "Probe"). It never calls DELETE /data,
which would wipe everything tied to your key.

Questions it answers:
  Q1  Can we set purchase_date in the past?   (needed to seed "last week" spending)
  Q2  Where does category live: purchase or merchant, string or list?
  Q3  What status does a new transfer get, and does it change after a re-fetch?
  Q4  Do account balances change after a purchase / transfer?
  Q5  Do bills work (create + list)?
  Q6  Does HTTPS work for the base URL?

The request bodies follow the documented Nessie schema as best we know it. If Nessie
rejects a field, the probe prints its error message: that's a finding too.

Run:  python -m ledgervoice.smoke.probe_nessie --yes
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, timedelta
from typing import Any

import httpx
from nessie_tools import NessieClient, NessieError

from ledgervoice.config import get_settings

ADDRESS = {
    "street_number": "100",
    "street_name": "Probe St",
    "city": "Arlington",
    "state": "VA",
    "zip": "22201",
}


class Probe:
    def __init__(self, client: NessieClient) -> None:
        self.c = client
        self.log: list[dict[str, Any]] = []

    def call(self, label: str, method: str, path: str, body: Any = None) -> Any:
        entry: dict[str, Any] = {"step": label, "request": f"{method} {path}", "body": body}
        try:
            resp = self.c.request(method, path, json=body)
            entry["ok"] = True
            entry["response"] = resp
        except NessieError as exc:  # message is already key-redacted
            entry["ok"] = False
            entry["error"] = str(exc)
            resp = None
        self.log.append(entry)
        status = "ok " if entry["ok"] else "ERR"
        print(f"  [{status}] {label}: {method} {path}")
        if not entry["ok"]:
            print(f"        {entry['error'][:300]}")
        return resp


def created_id(resp: Any) -> str | None:
    """Nessie POSTs usually return {"code": 201, "objectCreated": {"_id": ...}}."""
    if isinstance(resp, dict):
        obj = resp.get("objectCreated") or resp
        return obj.get("_id") if isinstance(obj, dict) else None
    return None


def balance(p: Probe, account_id: str | None, label: str) -> Any:
    if not account_id:
        return None
    acct = p.call(f"read {label} balance", "GET", f"/accounts/{account_id}")
    return acct.get("balance") if isinstance(acct, dict) else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Nessie sandbox behavior (writes data)")
    parser.add_argument("--yes", action="store_true", help="confirm writing probe data")
    args = parser.parse_args()
    if not args.yes:
        print("This writes test data to your Nessie sandbox. Re-run with --yes to proceed.")
        return 2

    logging.getLogger("httpx").setLevel(logging.WARNING)
    s = get_settings()
    s.require("nessie_api_key")
    assert s.nessie_api_key is not None
    key = s.nessie_api_key.get_secret_value()
    findings: dict[str, Any] = {}
    past = (date.today() - timedelta(days=10)).isoformat()
    today = date.today().isoformat()

    with NessieClient(key, base_url=s.nessie_base_url) as client:
        p = Probe(client)

        print("Creating probe customer + accounts")
        customer_body = {"first_name": "Pat", "last_name": "Probe", "address": ADDRESS}
        cust = created_id(p.call("create customer", "POST", "/customers", customer_body))
        checking = savings = None
        if cust:
            checking_body = {
                "type": "Checking",
                "nickname": "Probe Checking",
                "rewards": 0,
                "balance": 1000,
            }
            savings_body = {
                "type": "Savings",
                "nickname": "Probe Savings",
                "rewards": 0,
                "balance": 500,
            }
            path = f"/customers/{cust}/accounts"
            checking = created_id(p.call("create checking", "POST", path, checking_body))
            savings = created_id(p.call("create savings", "POST", path, savings_body))

        print("Q2: merchant category")
        merchant_body = {
            "name": "Probe Coffee Downtown",
            "category": ["Coffee Shop"],
            "address": ADDRESS,
            "geocode": {"lat": 38.88, "lng": -77.10},
        }
        merchant = created_id(p.call("create merchant", "POST", "/merchants", merchant_body))
        if merchant:
            m = p.call("read merchant", "GET", f"/merchants/{merchant}")
            findings["Q2_merchant_category"] = m.get("category") if isinstance(m, dict) else None

        print("Q1/Q4: back-dated purchase")
        bal_before = balance(p, checking, "checking")
        if checking and merchant:
            purchase_body = {
                "merchant_id": merchant,
                "medium": "balance",
                "purchase_date": past,
                "amount": 4.75,
                "description": "Probe latte",
            }
            p.call(
                "create purchase (10 days ago)",
                "POST",
                f"/accounts/{checking}/purchases",
                purchase_body,
            )
            purchases = p.call("list purchases", "GET", f"/accounts/{checking}/purchases")
            if isinstance(purchases, list) and purchases:
                first = purchases[0]
                findings["Q1_purchase_date_sent"] = past
                findings["Q1_purchase_date_stored"] = first.get("purchase_date")
                findings["Q2_purchase_fields"] = sorted(first.keys())
                findings["purchase_status"] = first.get("status")
        findings["Q4_checking_balance_before_purchase"] = bal_before
        findings["Q4_checking_balance_after_purchase"] = balance(p, checking, "checking")

        print("Q3/Q4: transfer checking -> savings")
        if checking and savings:
            sav_before = balance(p, savings, "savings")
            transfer_body = {
                "medium": "balance",
                "payee_id": savings,
                "amount": 50,
                "transaction_date": today,
                "description": "Probe transfer",
            }
            resp = p.call(
                "create transfer", "POST", f"/accounts/{checking}/transfers", transfer_body
            )
            tid = created_id(resp)
            if isinstance(resp, dict):
                obj = resp.get("objectCreated") or {}
                findings["Q3_transfer_status_on_create"] = obj.get("status")
            if tid:
                t = p.call("re-read transfer", "GET", f"/transfers/{tid}")
                findings["Q3_transfer_status_on_reread"] = (
                    t.get("status") if isinstance(t, dict) else None
                )
            findings["Q4_savings_before_transfer"] = sav_before
            findings["Q4_savings_after_transfer"] = balance(p, savings, "savings")
            findings["Q4_checking_after_transfer"] = balance(p, checking, "checking")

        print("Q5: bills")
        if checking:
            bill_body = {
                "status": "pending",
                "payee": "Probe Electric",
                "nickname": "electric",
                "payment_date": (date.today() + timedelta(days=8)).isoformat(),
                "recurring_date": 25,
                "payment_amount": 82.5,
            }
            p.call("create bill", "POST", f"/accounts/{checking}/bills", bill_body)
            bills = p.call("list bills", "GET", f"/accounts/{checking}/bills")
            findings["Q5_bills_listed"] = len(bills) if isinstance(bills, list) else None

    print("Q6: HTTPS")
    https_url = s.nessie_base_url.replace("http://", "https://", 1)
    try:
        r = httpx.get(f"{https_url}/customers", params={"key": key}, timeout=8)
        findings["Q6_https_status"] = r.status_code
    except httpx.HTTPError as exc:
        findings["Q6_https_status"] = f"failed: {type(exc).__name__}"

    out = s.artifacts_dir / "nessie_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    text = s.scrub(json.dumps({"findings": findings, "log": p.log}, indent=2, default=str))
    out.write_text(text, encoding="utf-8")

    print("\nFindings:")
    for k, v in findings.items():
        print(f"  {k}: {v}")
    print(f"\nFull request/response log: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
