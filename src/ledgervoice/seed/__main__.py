"""Command line for seed plans (offline; writes nothing to any bank).

    python -m ledgervoice.seed plan [--seed 42] [--anchor 2026-11-23] [--out DIR]

Writes plan.json (the full plan, canonical JSON) and truth-summary.json (balances,
spending, bills per customer) to DIR (default: settings.seed_dir, .data/seed). The
anchor date is --anchor, else LEDGERVOICE_TODAY, else the real date (printed, so a run
can always be repeated exactly).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from ledgervoice.seed.generator import generate
from ledgervoice.seed.truth import GroundTruth

DEFAULT_SEED = 42


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ledgervoice.seed")
    sub = parser.add_subparsers(dest="command", required=True)
    plan_cmd = sub.add_parser("plan", help="generate a plan and its ground truth (offline)")
    plan_cmd.add_argument("--seed", type=int, default=DEFAULT_SEED)
    plan_cmd.add_argument("--anchor", type=date.fromisoformat, default=None)
    plan_cmd.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    from ledgervoice.config import get_settings

    settings = get_settings()
    anchor: date = args.anchor or settings.ledgervoice_today or date.today()
    out: Path = args.out or settings.seed_dir

    plan = generate(args.seed, anchor)
    truth = GroundTruth(plan)
    out.mkdir(parents=True, exist_ok=True)
    (out / "plan.json").write_text(plan.to_json() + "\n")
    (out / "truth-summary.json").write_text(json.dumps(truth.summary(), indent=2) + "\n")

    last_day = plan.anchor - timedelta(days=1)
    print(f"seed {plan.seed}, today {plan.anchor}, history {plan.start}..{last_day}")
    print(f"digest {plan.digest()}")
    print(
        f"{len(plan.customers)} customers, {len(plan.accounts)} accounts, "
        f"{len(plan.merchants)} merchants, {len(plan.transactions)} transactions, "
        f"{len(plan.bills)} bills"
    )
    print(f"wrote {out / 'plan.json'} and {out / 'truth-summary.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
