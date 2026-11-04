"""T-371 A1: a locked state.db is busy, not corrupt (no quarantine); corrupt still quarantines."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from llmhub.clock import FakeClock
from llmhub.persistence.db import connect, migrate
from llmhub.persistence.integrity import StateDbBusy, open_state_db


def _healthy(path: Path) -> None:
    conn = connect(path)
    migrate(conn)
    conn.execute("INSERT INTO incidents(ts, kind, severity, details_json) VALUES (1,'k','info','')")
    conn.close()


def _exclusive_holder(path: Path) -> sqlite3.Connection:
    """Another process-like connection holding an exclusive lock (locking_mode=EXCLUSIVE)."""
    other = sqlite3.connect(str(path), isolation_level=None, timeout=0)
    other.execute("PRAGMA locking_mode=EXCLUSIVE")
    other.execute("BEGIN EXCLUSIVE")
    other.execute("INSERT INTO incidents(ts, kind, severity, details_json) VALUES (2,'k','info','')")
    other.execute("COMMIT")  # the exclusive lock stays until close
    return other


def test_locked_db_raises_busy_and_is_not_quarantined(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _healthy(path)
    holder = _exclusive_holder(path)
    sleeps: list[float] = []
    try:
        with pytest.raises(StateDbBusy):
            open_state_db(path, FakeClock(), busy_wait_s=3.0, sleep=sleeps.append)
    finally:
        holder.close()
    assert not list(tmp_path.glob("state.db.corrupt-*"))
    assert sum(sleeps) >= 3.0 and len(sleeps) >= 2
    r = open_state_db(path, FakeClock())
    assert r.safe_mode is False
    assert r.conn.execute("SELECT count(*) FROM incidents").fetchone()[0] == 2


def test_lock_released_during_wait_opens_normally(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _healthy(path)
    holder: list[sqlite3.Connection] = [_exclusive_holder(path)]

    def sleep(_s: float) -> None:
        if holder:
            holder.pop().close()

    r = open_state_db(path, FakeClock(), busy_wait_s=30.0, sleep=sleep)
    assert (r.safe_mode, r.reason) == (False, None)
    assert not list(tmp_path.glob("state.db.corrupt-*"))


def test_busy_from_migrate_write_is_busy_too(tmp_path: Path) -> None:
    """WAL: readers pass, but the schema_version write in migrate() hits a writer lock."""
    path = tmp_path / "state.db"
    _healthy(path)
    writer = sqlite3.connect(str(path), isolation_level=None, timeout=0)
    writer.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(StateDbBusy):
            open_state_db(path, FakeClock(), busy_wait_s=1.0, sleep=lambda _s: None)
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert not list(tmp_path.glob("state.db.corrupt-*"))


def test_corrupt_db_still_quarantined(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    path.write_bytes(b"this is not a sqlite database" * 200)
    r = open_state_db(path, FakeClock(), sleep=lambda _s: pytest.fail("no wait on corruption"))
    assert r.safe_mode is True and r.reason == "db_corrupt"
    assert list(tmp_path.glob("state.db.corrupt-*"))


def test_other_operational_error_is_raised_not_quarantined(tmp_path: Path) -> None:
    """Unreadable file (permissions, directory instead of file): no quarantine, error raised."""
    path = tmp_path / "state.db"
    path.mkdir()
    with pytest.raises(sqlite3.OperationalError):
        open_state_db(path, FakeClock(), sleep=lambda _s: None)
    assert path.is_dir()
    assert not list(tmp_path.glob("state.db.corrupt-*"))


def test_state_db_busy_is_exported() -> None:
    from llmhub import persistence

    assert persistence.StateDbBusy is StateDbBusy
    assert issubclass(StateDbBusy, RuntimeError)
