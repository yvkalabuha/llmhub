"""Daily consistent copies of state.db (sqlite3 online backup). No auto-restore (Q4)."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from llmhub.clock import Clock
from llmhub.persistence import state

_PREFIX = "state-"
_DAY_S = 86400.0


def backup(conn: sqlite3.Connection, dst_dir: Path, clock: Clock, keep: int = 7) -> Path:
    dst_dir = Path(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    now = clock.now()
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now)) + f"{int(now * 1000) % 1000:03d}"
    dst = dst_dir / f"{_PREFIX}{stamp}.db"
    out = sqlite3.connect(str(dst))
    try:
        conn.backup(out)
    finally:
        out.close()
    copies = sorted(dst_dir.glob(f"{_PREFIX}*.db"))
    for old in copies[: max(0, len(copies) - keep)]:
        old.unlink()
    return dst


def maybe_daily_backup(conn: sqlite3.Connection, dst_dir: Path, clock: Clock) -> Path | None:
    now = clock.now()
    last = state.get(conn, "last_backup_at")
    if last is not None and now - float(last) < _DAY_S:
        return None
    path = backup(conn, dst_dir, clock)
    state.set(conn, "last_backup_at", repr(now), now)
    return path
