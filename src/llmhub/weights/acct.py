"""Validator-private state (T-370 stream D, plan 2a item 2).

A tiny SQLite DB in `auth_state_dir` (not the controller DB): first-seen client IP per op and
the read position/served bytes of nginx's `weights-acct.log` (lines: `<op> <bytes> <status>`).
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_LINE = re.compile(r"^([0-9a-f]{12}) (\d+) (\d{3})$")
_COUNTED = frozenset({"200", "206"})  # nginx logs only these too (SEC-4); defence in depth
READ_CHUNK = 1024 * 1024  # bounded memory per read of the accounting log (SEC-2)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS ip_bind (op TEXT PRIMARY KEY, ip TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS served (op TEXT PRIMARY KEY, bytes INTEGER NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS logpos (id INTEGER PRIMARY KEY CHECK (id = 1),
  inode INTEGER NOT NULL, off INTEGER NOT NULL) STRICT;
"""


class AcctState:
    """Thread-safe store; one short-lived transaction per call."""

    def __init__(self, state_dir: Path, acct_log: Path) -> None:
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._path = state_dir / "auth-state.db"
        self._log = acct_log
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(_SCHEMA)
        os.chmod(self._path, 0o600)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(str(self._path), timeout=1.0, isolation_level=None)
        try:
            c.execute("PRAGMA journal_mode=WAL")
            yield c
        finally:
            c.close()

    def bind_ip(self, op: str, ip: str) -> bool:
        """Remember the first IP of `op`; True if `ip` is (or becomes) the bound one."""
        with self._lock, self._conn() as c:
            c.execute("INSERT OR IGNORE INTO ip_bind(op, ip) VALUES (?, ?)", (op, ip))
            row = c.execute("SELECT ip FROM ip_bind WHERE op = ?", (op,)).fetchone()
        return bool(row and row[0] == ip)

    def served(self, op: str) -> int:
        """Bytes already sent for `op`; reads the new tail of the accounting log first."""
        with self._lock, self._conn() as c:
            self._refresh(c)
            row = c.execute("SELECT bytes FROM served WHERE op = ?", (op,)).fetchone()
        return int(row[0]) if row else 0

    def _refresh(self, c: sqlite3.Connection) -> None:
        """Add the new complete lines of the log; reads at most READ_CHUNK bytes at a time."""
        try:
            f = self._log.open("rb")
        except OSError:
            return
        with f:
            st = os.fstat(f.fileno())
            pos = c.execute("SELECT inode, off FROM logpos WHERE id = 1").fetchone()
            off = 0
            if pos is not None and pos[0] == st.st_ino and pos[1] <= st.st_size:
                off = int(pos[1])  # same file: continue; rotated/truncated: from the start
            start = off
            add: dict[str, int] = {}
            carry = b""
            while True:
                try:
                    f.seek(off + len(carry))
                    data = f.read(READ_CHUNK)
                except OSError:
                    break
                if not data:
                    break
                buf = carry + data
                end = buf.rfind(b"\n")
                if end < 0:
                    if len(buf) >= READ_CHUNK:  # an over-long line is garbage: skip it whole
                        off += len(buf)
                        carry = b""
                        continue
                    carry = buf  # incomplete tail: wait for the rest
                    continue
                for raw in buf[: end + 1].decode("utf-8", "replace").splitlines():
                    m = _LINE.match(raw.strip())
                    if m is None or m.group(3) not in _COUNTED:
                        continue
                    add[m.group(1)] = add.get(m.group(1), 0) + int(m.group(2))
                off += end + 1
                carry = buf[end + 1 :]
                if len(carry) >= READ_CHUNK:
                    off += len(carry)
                    carry = b""
        if off == start and not add:
            return
        c.execute("BEGIN IMMEDIATE")
        for op, n in add.items():
            c.execute(
                "INSERT INTO served(op, bytes) VALUES (?, ?) "
                "ON CONFLICT(op) DO UPDATE SET bytes = bytes + excluded.bytes",
                (op, n),
            )
        c.execute(
            "INSERT INTO logpos(id, inode, off) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET inode = excluded.inode, off = excluded.off",
            (st.st_ino, off),
        )
        c.execute("COMMIT")
