"""SQLite access: connection pragmas, short write transactions, migrations."""

from __future__ import annotations

import contextvars
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_in_tx: contextvars.ContextVar[bool] = contextvars.ContextVar("llmhub_in_tx", default=False)


class Busy(Exception):
    """The database is locked by another writer; retry the action on the next step."""


def connect(path: Path | str, busy_timeout_ms: int = 300) -> sqlite3.Connection:
    """Open a connection. Controller: 250-500 ms; reaper: 5000 ms (DbConfig)."""
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=busy_timeout_ms / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT; ROLLBACK on error. No `await` allowed inside."""
    assert not _in_tx.get(), "nested tx is not allowed"
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if _is_busy(exc):
            raise Busy(str(exc)) from exc
        raise
    token = _in_tx.set(True)
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        _in_tx.reset(token)


def assert_not_in_tx() -> None:
    assert not _in_tx.get(), "await/sleep/adapter call inside a DB transaction"


def migrate(conn: sqlite3.Connection) -> int:
    """Apply migrations/NNNN_*.sql by PRAGMA user_version; mirror it in controller_state."""
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    for f in files:
        m = re.match(r"(\d+)_", f.name)
        if m is None:
            continue
        version = int(m.group(1))
        if version <= current:
            continue
        script = f.read_text(encoding="utf-8")
        conn.executescript(f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version={version};\nCOMMIT;")
        current = version
    if current:
        with tx(conn):
            conn.execute(
                "INSERT INTO controller_state(key, value, updated_at)"
                " VALUES ('schema_version', ?, CAST(strftime('%s','now') AS REAL))"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(current),),
            )
    return current
