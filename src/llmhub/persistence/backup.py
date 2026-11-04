"""Daily consistent copies of state.db (sqlite3 online backup). No auto-restore (Q4)."""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import UTC, datetime
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


def _backup_order(p: Path) -> tuple[str, int]:
    """state-<stamp>.db < state-<stamp>-1.db < state-<stamp>-2.db (name order breaks on -10)."""
    stem = p.stem.removeprefix(_PREFIX)
    base, _, n = stem.partition("-")
    return base, int(n) if n.isdigit() else 0


def backup_state_db(src: Path, dst_dir: Path, keep: int, now: datetime) -> Path:
    """`llmhub backup` (T-371 A2): online copy of `src` into `dst_dir/state-<UTC stamp>.db`.

    The copy goes to `*.tmp` first, must pass `integrity_check`, then is moved in place with
    `os.replace` (mode 0640). Copies made in the same second get a `-N` suffix instead of
    overwriting. Only `state-*.db` files are rotated (newest `keep` stay)."""
    if keep < 1:
        raise ValueError("keep must be >= 1")
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(str(src))
    dst_dir = Path(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%S")
    dst = dst_dir / f"{_PREFIX}{stamp}.db"
    n = 0
    while dst.exists():
        n += 1
        dst = dst_dir / f"{_PREFIX}{stamp}-{n}.db"
    tmp = dst.with_name(dst.name + ".tmp")
    # mode=rw: never create a missing source; the online backup API sees committed data only
    source = sqlite3.connect(f"file:{src}?mode=rw", uri=True, timeout=30)
    try:
        out = sqlite3.connect(str(tmp))
        try:
            source.backup(out)
            row = out.execute("PRAGMA integrity_check").fetchone()
            if row is None or row[0] != "ok":
                raise sqlite3.DatabaseError("backup copy failed integrity_check")
        finally:
            out.close()
        os.chmod(tmp, 0o640)
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        source.close()
    copies = sorted(dst_dir.glob(f"{_PREFIX}*.db"), key=_backup_order)
    for old in copies[: max(0, len(copies) - keep)]:
        old.unlink()
    return dst
