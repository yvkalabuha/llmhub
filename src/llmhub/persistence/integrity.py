"""Opening state.db with an integrity check; corruption leads to safe mode (HALTED).

T-371 A1: a *locked* database ("database is locked"/"busy", e.g. the reaper migrating or a
backup holding a lock) is not corruption: the open is retried for `busy_wait_s` and then
`StateDbBusy` is raised (serve exits with code 75, Docker restarts it). Other operational
errors (unreadable file, permissions) are raised as they are. Only `sqlite3.DatabaseError`
proper ("file is not a database", "malformed") quarantines the file.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from llmhub.clock import Clock
from llmhub.persistence import state
from llmhub.persistence.db import Busy, connect, migrate, tx

BUSY_STEP_S = 1.0


class StateDbBusy(RuntimeError):
    """state.db stayed locked by another connection for the whole wait; nothing was moved."""


class OpenResult(NamedTuple):
    conn: sqlite3.Connection
    safe_mode: bool
    reason: str | None


class _Locked(Exception):
    pass


def _is_lock_error(exc: BaseException) -> bool:
    if isinstance(exc, Busy):
        return True
    if isinstance(exc, sqlite3.OperationalError):
        msg = str(exc).lower()
        return "locked" in msg or "busy" in msg
    return False


def _try_open(path: Path) -> sqlite3.Connection | None:
    """Open + migrate + integrity_check; None when the file is damaged, `_Locked` when it is
    locked; any other operational error is raised unchanged (never a quarantine)."""
    conn: sqlite3.Connection | None = None
    try:
        conn = connect(path)
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        if len(rows) != 1 or rows[0][0] != "ok":
            conn.close()
            return None
        migrate(conn)
        return conn
    except (sqlite3.DatabaseError, Busy) as exc:
        if conn is not None:
            conn.close()
        if _is_lock_error(exc):
            raise _Locked from None
        if isinstance(exc, sqlite3.OperationalError):
            raise
        return None


def _open_waiting(
    path: Path, busy_wait_s: float, sleep: Callable[[float], None]
) -> sqlite3.Connection | None:
    waited = 0.0
    while True:
        try:
            return _try_open(path)
        except _Locked:
            if waited >= busy_wait_s:
                raise StateDbBusy(f"{path.name} is locked (waited {waited:g}s)") from None
            step = min(BUSY_STEP_S, max(busy_wait_s - waited, 0.0)) or BUSY_STEP_S
            sleep(step)
            waited += step


def _quarantine(path: Path, ts: int) -> Path:
    dst = path.with_name(f"{path.name}.corrupt-{ts}")
    path.rename(dst)
    for suffix in ("-wal", "-shm"):
        side = path.with_name(path.name + suffix)
        if side.exists():
            side.rename(dst.with_name(dst.name + suffix))
    return dst


def open_state_db(
    path: Path,
    clock: Clock,
    *,
    busy_wait_s: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
) -> OpenResult:
    path = Path(path)
    if path.exists():
        conn = _open_waiting(path, busy_wait_s, sleep)
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
