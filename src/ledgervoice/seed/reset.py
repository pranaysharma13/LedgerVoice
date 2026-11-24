"""Reset a seed: delete exactly the accounts it created (M1c-2, D36, D42, D51).

Safety rules:
- Only account ids listed in the manifest are deleted; nothing is looked up or
  guessed, so no other account in the shared sandbox can be touched.
- A manifest written for a different server is refused.
- DELETE /data is never called. Merchants and customers stay (they are reused, D51);
  transactions and bills stay as orphans under the deleted accounts (D36), which is
  why the next seed always creates fresh accounts.
- An account that is already gone counts as deleted. Any other failure leaves the
  manifest unchanged, so running reset again finishes the job.
- The seed's audit database is moved to the archive, so the next seed starts fresh.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from nessie_tools import BankBackend, BankError, NotFoundError

from ledgervoice.seed.apply import SeedError
from ledgervoice.seed.manifest import Manifest, SeedStatus


def reset(
    backend: BankBackend,
    manifest: Manifest,
    *,
    server: str,
    archive_dir: Path | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[str]:
    """Delete the manifest's accounts. Returns problems; empty means the reset is done
    and the manifest (updated in place) is marked RESET."""
    if manifest.server != server:
        raise SeedError(
            f"this manifest belongs to {manifest.server!r}, not {server!r}; refusing to reset"
        )
    if manifest.status is SeedStatus.RESET:
        return []
    problems: list[str] = []
    for key, account_id in sorted(manifest.accounts.items()):
        try:
            backend.delete_account(account_id)
        except NotFoundError:
            pass  # already gone: that's the goal
        except BankError as exc:
            problems.append(f"{key}: could not delete {account_id}: {exc}")
    if problems:
        return problems
    if manifest.audit_db and archive_dir is not None:
        audit_path = Path(manifest.audit_db)
        if audit_path.exists():
            archive_dir.mkdir(parents=True, exist_ok=True)
            target = archive_dir / audit_path.name
            shutil.move(str(audit_path), str(target))
            manifest.audit_db = str(target)
    manifest.status = SeedStatus.RESET
    manifest.reset_at = now().isoformat(timespec="seconds")
    return problems
