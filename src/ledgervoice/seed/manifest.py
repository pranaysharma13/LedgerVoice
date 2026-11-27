"""What a seed run wrote, and where (M1c-2).

Manifest: one seed run. Maps every plan key to the id the bank assigned, plus the
seed, date, plan digest, the server it went to and a status. It is saved after every
write, so an aborted seed (D42) still lists everything reset has to delete.

Registry: ids worth reusing across seed runs on the same server. Merchants can't be
deleted in Nessie and customers hold no money, so both are reused instead of piling
up in the shared sandbox (D51). Every reuse is checked against the bank first.

SeedStore keeps both as JSON files in one directory (default .data/seed/).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA = 1


class SeedStatus(StrEnum):
    PARTIAL = "partial"  # writing, or aborted part-way: reset before seeding again
    COMPLETE = "complete"  # every planned record was written
    RESET = "reset"  # the accounts were deleted; a new seed may start


@dataclass
class Manifest:
    server: str  # "fake" or the Nessie base URL; reset refuses a different server
    seed: int
    anchor: date
    digest: str  # plan digest; verify regenerates the plan and compares
    started_at: str
    status: SeedStatus = SeedStatus.PARTIAL
    finished_at: str | None = None
    reset_at: str | None = None
    audit_db: str | None = None  # fresh audit database for this seed (R1-07)
    merchants: dict[str, str] = field(default_factory=dict)
    customers: dict[str, str] = field(default_factory=dict)
    accounts: dict[str, str] = field(default_factory=dict)
    transactions: dict[str, str] = field(default_factory=dict)
    bills: dict[str, str] = field(default_factory=dict)
    timing: dict[str, float] = field(default_factory=dict)
    schema: int = MANIFEST_SCHEMA

    def customer_ids(self) -> list[str]:
        return sorted(self.customers.values())

    def to_json(self) -> str:
        data = asdict(self)
        data["anchor"] = self.anchor.isoformat()
        data["status"] = self.status.value
        return json.dumps(data, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> Manifest:
        data: dict[str, Any] = json.loads(text)
        if data.get("schema") != MANIFEST_SCHEMA:
            raise ValueError(f"unsupported manifest schema {data.get('schema')!r}")
        data["anchor"] = date.fromisoformat(data["anchor"])
        data["status"] = SeedStatus(data["status"])
        return cls(**data)


ABANDONED = "abandoned_customers"


@dataclass
class Registry:
    """server -> kind ("merchants" | "customers") -> plan key -> bank id."""

    servers: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)

    def get(self, server: str, kind: str, key: str) -> str | None:
        return self.servers.get(server, {}).get(kind, {}).get(key)

    def set(self, server: str, kind: str, key: str, bank_id: str) -> None:
        self.servers.setdefault(server, {}).setdefault(kind, {})[key] = bank_id

    def abandon(self, server: str, customer_id: str, note: str) -> None:
        """Remember a customer that was not reused because it still had accounts
        (V2-06): kind "abandoned_customers", customer id -> what it still holds."""
        self.set(server, ABANDONED, customer_id, note)

    def abandoned(self, server: str) -> dict[str, str]:
        return dict(self.servers.get(server, {}).get(ABANDONED, {}))

    def to_json(self) -> str:
        return json.dumps(self.servers, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> Registry:
        return cls(servers=json.loads(text))


class SeedStore:
    """The manifest, the registry and archived audit databases, in one directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.manifest_path = directory / "manifest.json"
        self.registry_path = directory / "registry.json"
        self.archive_dir = directory / "archive"
        self.audit_dir = directory / "audit"

    def load_manifest(self) -> Manifest | None:
        if not self.manifest_path.exists():
            return None
        return Manifest.from_json(self.manifest_path.read_text())

    def save_manifest(self, manifest: Manifest) -> None:
        _write_atomically(self.manifest_path, manifest.to_json() + "\n")

    def load_registry(self) -> Registry:
        if not self.registry_path.exists():
            return Registry()
        return Registry.from_json(self.registry_path.read_text())

    def save_registry(self, registry: Registry) -> None:
        _write_atomically(self.registry_path, registry.to_json() + "\n")


def _write_atomically(path: Path, text: str) -> None:
    """Write to a temp file, flush it to disk, then rename: a crash never leaves half a
    manifest, and after a power loss the file is the old or the new version (R2-19)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    _fsync_directory(path.parent)


def _fsync_directory(directory: Path) -> None:
    """Make the rename itself durable (POSIX). Not possible on every platform; there
    the rename is still atomic, just not guaranteed to survive a power loss."""
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)
