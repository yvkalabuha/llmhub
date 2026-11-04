"""Shared storage of the mock "cloud" so the reaper process sees the same resources."""

from __future__ import annotations

import sqlite3
from pathlib import Path

_DDL = """
CREATE TABLE IF NOT EXISTS resources (
  ref TEXT PRIMARY KEY, label TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'instance',
  created_at REAL NOT NULL, boot_s REAL NOT NULL, price_usd_h REAL NOT NULL,
  disk_usd_h REAL NOT NULL DEFAULT 0, host_id TEXT NOT NULL,
  destroyed_at REAL, status_override TEXT
) STRICT;
"""


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
