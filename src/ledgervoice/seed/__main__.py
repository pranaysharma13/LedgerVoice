"""Command line for seed data.

    python -m ledgervoice.seed plan   [--seed 42] [--anchor YYYY-MM-DD] [--out DIR]
    python -m ledgervoice.seed apply  [--seed 42] [--anchor YYYY-MM-DD]   # writes to Nessie
    python -m ledgervoice.seed verify                                    # reads Nessie
    python -m ledgervoice.seed reset                                     # deletes seed accounts

`plan` is offline: it writes plan.json and truth-summary.json (default: .data/seed).
`apply` refuses to run while a seed is in place; it writes the plan to Nessie, saving the
manifest after every write, then verifies. If it aborts (D42), run `reset`, then `apply`.
`verify` regenerates the plan from the manifest's seed and date and checks the bank.
`reset` deletes exactly the accounts in the manifest (never DELETE /data).

The anchor date is --anchor, else LEDGERVOICE_TODAY, else the real date (printed).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

from nessie_tools import AuditLog, BankBackend, BankError

from ledgervoice.seed.apply import SeedError, apply_plan
from ledgervoice.seed.generator import generate
from ledgervoice.seed.manifest import Manifest, SeedStatus, SeedStore
from ledgervoice.seed.plan import SeedPlan
from ledgervoice.seed.reset import reset
from ledgervoice.seed.truth import GroundTruth
from ledgervoice.seed.verify import VerifyReport, verify

DEFAULT_SEED = 42
PROGRESS_EVERY = 50

# (backend, server name, close) for the live bank; tests inject a fake
BankFactory = Callable[[], tuple[BankBackend, str, Callable[[], None]]]


def main(argv: list[str] | None = None, bank_factory: BankFactory | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ledgervoice.seed")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("plan", "generate a plan and its ground truth (offline)"),
        ("apply", "write the plan to Nessie, then verify it"),
    ):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("--seed", type=int, default=DEFAULT_SEED)
        cmd.add_argument("--anchor", type=date.fromisoformat, default=None)
        cmd.add_argument("--out" if name == "plan" else "--dir", type=Path, default=None)
    for name, text in (
        ("verify", "check that Nessie holds exactly the seeded plan"),
        ("reset", "delete the seed's accounts so a new seed can start"),
    ):
        sub.add_parser(name, help=text).add_argument("--dir", type=Path, default=None)
    args = parser.parse_args(argv)

    from ledgervoice.config import get_settings

    settings = get_settings()
    if args.command == "plan":
        return _plan(args, settings.ledgervoice_today, args.out or settings.seed_dir)

    store = SeedStore(args.dir or settings.seed_dir)
    backend, server, close = (bank_factory or _live_bank)()
    try:
        if args.command == "apply":
            anchor = args.anchor or settings.ledgervoice_today or date.today()
            return _apply(args.seed, anchor, backend, server, store)
        if args.command == "verify":
            return _verify(backend, store)
        return _reset(backend, server, store)
    except SeedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    finally:
        close()


# --- commands ------------------------------------------------------------------
def _plan(args: argparse.Namespace, today: date | None, out: Path) -> int:
    anchor: date = args.anchor or today or date.today()
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


def _apply(seed: int, anchor: date, backend: BankBackend, server: str, store: SeedStore) -> int:
    current = store.load_manifest()
    if current is not None and current.status is not SeedStatus.RESET:
        raise SeedError(
            f"a seed is already in place ({current.status.value}, seed {current.seed}, "
            f"{current.anchor}); run `make reset-live` first"
        )
    plan = generate(seed, anchor)
    print(f"seeding seed {seed} for {anchor} into {server} (digest {plan.digest()[:12]})")

    def progress(done: int, total: int) -> None:
        if done % PROGRESS_EVERY == 0 or done == total:
            print(f"  {done}/{total}", flush=True)

    registry = store.load_registry()
    try:
        manifest = apply_plan(
            plan,
            backend,
            server=server,
            registry=registry,
            save=store.save_manifest,
            save_registry=store.save_registry,
            audit_dir=store.audit_dir,
            on_progress=progress,
        )
    except BankError as exc:
        print(
            f"aborted: {exc}\nNothing is retried (D42). Run `make reset-live`, then seed again.",
            file=sys.stderr,
        )
        return 1
    t = manifest.timing
    print(
        f"wrote {t.get('writes', 0):.0f} records in {t.get('total_s', 0)} s "
        f"(p50 {t.get('p50_ms')} ms, p95 {t.get('p95_ms')} ms, max {t.get('max_ms')} ms)"
    )
    return _report(_run_verify(plan_for(manifest), backend, manifest))


def _verify(backend: BankBackend, store: SeedStore) -> int:
    manifest = _require_manifest(store)
    return _report(_run_verify(plan_for(manifest), backend, manifest))


def _reset(backend: BankBackend, server: str, store: SeedStore) -> int:
    manifest = _require_manifest(store)
    if manifest.status is SeedStatus.RESET:
        print("already reset; nothing to do")
        return 0
    print(f"deleting {len(manifest.accounts)} seed accounts on {server} (never DELETE /data)")
    problems = reset(backend, manifest, server=server, archive_dir=store.archive_dir)
    if problems:
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print("reset incomplete; run it again", file=sys.stderr)
        return 1
    store.save_manifest(manifest)
    print("reset done; the next `make seed-live` starts with fresh accounts")
    return 0


# --- helpers -------------------------------------------------------------------
def plan_for(manifest: Manifest) -> SeedPlan:
    """The plan a manifest was written from (verify checks the digest matches)."""
    return generate(manifest.seed, manifest.anchor)


def _run_verify(plan: SeedPlan, backend: BankBackend, manifest: Manifest) -> VerifyReport:
    audit = AuditLog(manifest.audit_db) if manifest.audit_db else None
    try:
        return verify(plan, backend, manifest, audit=audit)
    finally:
        if audit is not None:
            audit.close()


def _report(report: VerifyReport) -> int:
    checked = ", ".join(f"{n} {k}" for k, n in report.checked.items())
    if report.ok:
        print(f"verified: {checked}")
        return 0
    print(f"VERIFICATION FAILED ({len(report.problems)} problems; checked {checked}):")
    for p in report.problems[:50]:
        print(f"  {p}")
    if len(report.problems) > 50:
        print(f"  ... and {len(report.problems) - 50} more")
    return 1


def _require_manifest(store: SeedStore) -> Manifest:
    manifest = store.load_manifest()
    if manifest is None:
        raise SeedError(f"no seed manifest in {store.directory}; run `make seed-live` first")
    return manifest


def _live_bank() -> tuple[BankBackend, str, Callable[[], None]]:
    from nessie_tools import NessieBackend, NessieClient

    from ledgervoice.config import get_settings

    settings = get_settings()
    settings.require("nessie_api_key")
    assert settings.nessie_api_key is not None
    client = NessieClient(
        settings.nessie_api_key.get_secret_value(), base_url=settings.nessie_base_url
    )
    return NessieBackend(client), settings.nessie_base_url, client.close


if __name__ == "__main__":
    sys.exit(main())
