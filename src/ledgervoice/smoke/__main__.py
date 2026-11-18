from __future__ import annotations

import argparse
import logging
import sys

from ledgervoice.config import get_settings
from ledgervoice.costs import CostLedger
from ledgervoice.smoke.checks import CHECKS, run_check


def main() -> int:
    parser = argparse.ArgumentParser(description="M0 smoke checks, one real call per API")
    parser.add_argument("checks", nargs="*", help=f"any of: {' '.join(CHECKS)} (default: all)")
    args = parser.parse_args()
    unknown = [n for n in args.checks if n not in CHECKS]
    if unknown:
        parser.error(f"unknown check(s): {', '.join(unknown)}; choose from {', '.join(CHECKS)}")
    names = args.checks or list(CHECKS)

    # httpx logs full request URLs at INFO, and Nessie's key is in the URL. Keep it quiet.
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = get_settings()
    ledger = CostLedger(settings.cost_ledger_path)
    print(f"run_id={ledger.run_id}  ledger={settings.cost_ledger_path}\n")

    failed = 0
    for name in names:
        r = run_check(name, settings, ledger)
        failed += not r.ok
        print(f"[{'PASS' if r.ok else 'FAIL'}] {r.name:<13} {r.summary}")
        for note in r.notes:
            print(f"{'':20}note: {note}")
    print(f"\n{len(names) - failed}/{len(names)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
