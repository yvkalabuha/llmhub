import sqlite3
from pathlib import Path

import pytest

from llmhub.persistence.db import connect, migrate

TABLES = {
    "controller_state", "clients", "instances", "jobs", "leases", "transitions", "ledger",
    "reservations", "provider_accounts", "provider_snapshots", "incidents", "host_blacklist",
    "cost_allocations",
}  # fmt: skip


def test_migrate_empty_db(tmp_path: Path) -> None:
    conn = connect(tmp_path / "state.db")
    assert migrate(conn) == 1
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    names = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if not r[0].startswith("sqlite_")
    }
    assert names == TABLES
    row = conn.execute("SELECT value FROM controller_state WHERE key='schema_version'").fetchone()
    assert row[0] == "1"


def test_migrate_twice_is_noop(tmp_path: Path) -> None:
    conn = connect(tmp_path / "state.db")
    migrate(conn)
    assert migrate(conn) == 1


def test_pragmas(tmp_path: Path) -> None:
    conn = connect(tmp_path / "state.db", busy_timeout_ms=300)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 300
    assert conn.isolation_level is None


def test_memory_db() -> None:
    conn = connect(":memory:")
    assert migrate(conn) == 1


def test_check_rejects_bad_state() -> None:
    conn = connect(":memory:")
    migrate(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO instances(id, op_id, pool, provider, label, state, state_since,"
            " created_at, max_lifetime_at) VALUES ('i','o','p','mock','l','BOGUS',0,0,0)"
        )
