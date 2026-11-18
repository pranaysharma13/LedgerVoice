"""Per-call cost ledger (JSONL).

Every external API call records one line: which service, what it consumed in the
provider's own billing units (tokens, characters, audio seconds, requests), how long
it took, and an estimated USD cost if we know the price.

Why raw units first: prices change and Cartesia bills in credits, so the units are the
durable truth. Dollar estimates are derived from PRICING, which you fill in from each
provider's pricing page. Unknown prices are reported as "unpriced", never guessed.

Usage:
    ledger = CostLedger(path)
    ledger.record("anthropic", "messages.create", {"input_tokens": 512, "output_tokens": 40})
    python -m ledgervoice.costs          # print totals
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

# USD per single unit, keyed by (service, unit). None = not yet verified -> unpriced.
# Fill these in from the providers' pricing pages and note the date you checked.
PRICING: dict[tuple[str, str], float | None] = {
    ("anthropic", "input_tokens"): None,
    ("anthropic", "output_tokens"): None,
    ("anthropic", "cache_read_input_tokens"): None,
    ("anthropic", "cache_creation_input_tokens"): None,
    ("cartesia", "tts_characters"): None,
    ("cartesia", "stt_audio_seconds"): None,
    ("moss", "queries"): None,
    ("nessie", "requests"): 0.0,  # free sandbox
}

RUN_ID = os.environ.get("LEDGER_RUN_ID") or uuid.uuid4().hex[:8]


class CostLedger:
    def __init__(
        self,
        path: Path,
        run_id: str = RUN_ID,
        pricing: dict[tuple[str, str], float | None] | None = None,
    ) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.pricing = PRICING if pricing is None else pricing

    def estimate(self, service: str, units: dict[str, float]) -> tuple[float, list[str]]:
        usd = 0.0
        unpriced: list[str] = []
        for unit, amount in units.items():
            price = self.pricing.get((service, unit))
            if price is None:
                if amount:
                    unpriced.append(unit)
            else:
                usd += price * amount
        return usd, unpriced

    def record(
        self,
        service: str,
        operation: str,
        units: dict[str, float],
        latency_ms: float | None = None,
        **meta: Any,
    ) -> dict[str, Any]:
        usd, unpriced = self.estimate(service, units)
        row = {
            "ts": time.time(),
            "run_id": self.run_id,
            "service": service,
            "operation": operation,
            "units": units,
            "latency_ms": None if latency_ms is None else round(latency_ms, 1),
            "est_usd": round(usd, 6),
            "unpriced_units": unpriced,
            "meta": meta,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        return row


def summarize(path: Path) -> tuple[dict[str, dict[str, Any]], int]:
    """Totals per service (calls, summed units, estimated USD, units still unpriced),
    plus the number of malformed lines skipped (e.g. a write cut off by a crash)."""
    totals: dict[str, dict[str, Any]] = {}
    skipped = 0
    if not Path(path).exists():
        return totals, skipped
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        t = totals.setdefault(
            row["service"],
            {"calls": 0, "units": defaultdict(float), "est_usd": 0.0, "unpriced": set()},
        )
        t["calls"] += 1
        for unit, amount in row["units"].items():
            t["units"][unit] += amount
        t["est_usd"] += row["est_usd"]
        t["unpriced"].update(row["unpriced_units"])
    return totals, skipped


def main() -> None:
    from ledgervoice.config import get_settings

    path = Path(sys.argv[1]) if len(sys.argv) > 1 else get_settings().cost_ledger_path
    totals, skipped = summarize(path)
    if skipped:
        print(f"skipped {skipped} malformed line(s)")
    if not totals:
        print(f"No ledger entries at {path}")
        return
    for service, t in sorted(totals.items()):
        units = ", ".join(f"{k}={v:g}" for k, v in sorted(t["units"].items()))
        note = f"  (unpriced: {', '.join(sorted(t['unpriced']))})" if t["unpriced"] else ""
        print(f"{service:<10} calls={t['calls']:<4} ${t['est_usd']:.4f}  {units}{note}")


if __name__ == "__main__":
    main()
