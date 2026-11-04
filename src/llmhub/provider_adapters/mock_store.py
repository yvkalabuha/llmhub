"""Shared storage of the mock "cloud" so the reaper process sees the same resources."""

from __future__ import annotations

import sqlite3
from pathlib import Path

_DDL = """
CREATE TABLE IF NOT EXISTS resources (
  ref TEXT PRIMARY KEY, label TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'instance',
  created_at REAL NOT NULL, boot_s REAL NOT NULL, price_usd_h REAL NOT NULL,
  disk_usd_h REAL NOT NULL DEFAULT 0, host_id TEXT NOT NULL,
  destroyed_at REAL, status_override TEXT,
  visible_at REAL NOT NULL DEFAULT 0,    -- hidden from list_owned until then (late create)
  immortal INTEGER NOT NULL DEFAULT 0    -- DESTROY_NEVER: destroy is accepted, nothing happens
) STRICT;
"""

_COLUMNS = (
    "label", "kind", "created_at", "boot_s", "price_usd_h", "disk_usd_h", "host_id",
    "visible_at", "immortal",
)  # fmt: skip


class MockStore:
    """SQLite-backed store (file path for multi-process use, ":memory:" in unit tests)."""

    def __init__(self, path: Path | str = ":memory:") -> None:
        self.conn = sqlite3.connect(str(path), isolation_level=None, timeout=5.0)
        self.conn.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_DDL)

    def next_ref(self) -> str:
        n = self.conn.execute("SELECT count(*) FROM resources").fetchone()[0]
        return f"mock-{n + 1:06d}"

    def insert(self, **values: object) -> str:
        """Atomically allocate a ref and insert a row (safe with several connections)."""
        cols = {
            "kind": "instance", "disk_usd_h": 0.0, "boot_s": 0.0, "visible_at": 0.0,
            "immortal": 0,
        } | values  # fmt: skip
        names = [c for c in _COLUMNS if c in cols]
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            ref = self.next_ref()
            self.conn.execute(
                f"INSERT INTO resources(ref,{','.join(names)}) VALUES (?{',?' * len(names)})",
                (ref, *(cols[c] for c in names)),
            )
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        return ref
