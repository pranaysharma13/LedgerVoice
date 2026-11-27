"""M1c-2 live check: a small seed on real Nessie, end to end (runs in `make test-live`).

One persona, 15 transactions (33 writes): apply -> verify -> reset -> apply again ->
verify -> reset. The second apply must reuse the same merchants and customer (D51) and
use fresh accounts (D36). Its registry lives in .data/seed-live-test/, separate from the
real seed, so merchants are reused across test runs instead of piling up in the sandbox.

The manifest is captured after every write and reset in `finally`, so a run that
aborts part-way (a timeout, a failed assertion) still deletes the accounts it made
(R2-15). Reusing the customer also checks a live fact the fake can't: after reset,
the customer lists no accounts (R2-06 reuses only customers with none).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from nessie_tools import AuditLog, NessieBackend, NessieClient, NotFoundError

from ledgervoice.seed import generate
from ledgervoice.seed.apply import apply_plan
from ledgervoice.seed.manifest import Manifest, SeedStatus, SeedStore
from ledgervoice.seed.reset import reset
from ledgervoice.seed.verify import verify

pytestmark = pytest.mark.live
LIVE_TEST_DIR = Path(".data/seed-live-test")


def test_small_live_seed_round_trip() -> None:
    from ledgervoice.config import get_settings

    s = get_settings()
    s.require("nessie_api_key")
    assert s.nessie_api_key is not None
    client = NessieClient(s.nessie_api_key.get_secret_value(), base_url=s.nessie_base_url)
    backend, server = NessieBackend(client), s.nessie_base_url
    store = SeedStore(LIVE_TEST_DIR)
    plan = generate(42, date(2026, 11, 23)).subset("priya", 15)
    manifests: list[Manifest] = []
    try:
        for _ in range(2):
            registry = store.load_registry()
            current: list[Manifest] = []  # the manifest as of the last write
            try:
                manifest = apply_plan(
                    plan,
                    backend,
                    server=server,
                    registry=registry,
                    save=current.append,
                    save_registry=store.save_registry,
                )
                manifests.append(manifest)
                report = verify(plan, backend, manifest, audit=AuditLog(":memory:"))
                assert report.ok, report.problems
            finally:
                if current and current[-1].status is not SeedStatus.RESET:
                    assert reset(backend, current[-1], server=server) == []
            assert manifest.status is SeedStatus.RESET
            for account_id in manifest.accounts.values():
                with pytest.raises(NotFoundError):
                    backend.get_account(account_id)
            for customer_id in manifest.customers.values():
                assert backend.list_accounts(customer_id) == []  # nothing hidden left
    finally:
        client.close()
    first, second = manifests
    assert second.merchants == first.merchants  # reused (D51)
    assert second.customers == first.customers
    assert not set(second.accounts.values()) & set(first.accounts.values())  # fresh (D36)
    print(f"live seed timing: {first.timing}")
