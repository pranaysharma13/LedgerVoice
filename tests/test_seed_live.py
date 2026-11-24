"""M1c-2 live check: a small seed on real Nessie, end to end (runs in `make test-live`).

One persona, 15 transactions (33 writes): apply -> verify -> reset -> apply again ->
verify -> reset. The second apply must reuse the same merchants and customer (D51) and
use fresh accounts (D36). Its registry lives in .data/seed-live-test/, separate from the
real seed, so merchants are reused across test runs instead of piling up in the sandbox.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from nessie_tools import AuditLog, NessieBackend, NessieClient, NotFoundError

from ledgervoice.seed import generate
from ledgervoice.seed.apply import apply_plan
from ledgervoice.seed.manifest import SeedStatus, SeedStore
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
    manifests = []
    try:
        for _ in range(2):
            registry = store.load_registry()
            manifest = apply_plan(
                plan, backend, server=server, registry=registry, save_registry=store.save_registry
            )
            manifests.append(manifest)
            try:
                report = verify(plan, backend, manifest, audit=AuditLog(":memory:"))
                assert report.ok, report.problems
            finally:
                assert reset(backend, manifest, server=server) == []
            assert manifest.status is SeedStatus.RESET
            for account_id in manifest.accounts.values():
                with pytest.raises(NotFoundError):
                    backend.get_account(account_id)
    finally:
        client.close()
    first, second = manifests
    assert second.merchants == first.merchants  # reused (D51)
    assert second.customers == first.customers
    assert not set(second.accounts.values()) & set(first.accounts.values())  # fresh (D36)
    print(f"live seed timing: {first.timing}")
