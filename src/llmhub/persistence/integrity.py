"""Opening state.db with an integrity check; corruption leads to safe mode (HALTED)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import NamedTuple

from llmhub.clock import Clock
from llmhub.persistence import state
from llmhub.persistence.db import connect, migrate, tx


class OpenResult(NamedTuple):
    conn: sqlite3.Connection
    safe_mode: bool
    reason: str | None


def _try_open(path: Path) -> sqlite3.Connection | None:
    """Open + migrate + integrity_check; None when the file is damaged."""
    conn: sqlite3.Connection | None = None
    try:
        conn = connect(path)
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        if len(rows) != 1 or rows[0][0] != "ok":
            conn.close()
            return None
        migrate(conn)
        return conn
    except sqlite3.DatabaseError:
        if conn is not None:
            conn.close()
        return None


def _quarantine(path: Path, ts: int) -> Path:
    dst = path.with_name(f"{path.name}.corrupt-{ts}")
    path.rename(dst)
    for suffix in ("-wal", "-shm"):
        side = path.with_name(path.name + suffix)
        if side.exists():
            side.rename(dst.with_name(dst.name + suffix))
    return dst


def open_state_db(path: Path, clock: Clock) -> OpenResult:
    path = Path(path)
    if path.exists():
        conn = _try_open(path)
        if conn is not None:
            if state.get(conn, "safe_mode") == "1":
                return OpenResult(conn, True, state.get(conn, "halt_reason"))
            return OpenResult(conn, False, None)
        now = clock.now()
        moved = _quarantine(path, int(now))
        conn = connect(path)
        migrate(conn)
        state.halt(conn, "db_corrupt", clock)
        state.set(conn, "safe_mode", "1", now)
        with tx(conn):
            conn.execute(
                "INSERT INTO incidents(ts, kind, severity, ref, dedup_key, details_json)"
                " VALUES (?,?,?,?,?,?)",
                (now, "db_corrupt", "critical", None, f"db_corrupt:{int(now)}",
                 json.dumps({"quarantined_as": moved.name})),
            )  # fmt: skip
        return OpenResult(conn, True, "db_corrupt")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    migrate(conn)
    return OpenResult(conn, False, None)
