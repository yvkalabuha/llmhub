import sqlite3
from pathlib import Path

import pytest

from llmhub.clock import FakeClock
from llmhub.persistence import state
from llmhub.persistence.backup import backup, maybe_daily_backup
from llmhub.persistence.db import connect, migrate
from llmhub.persistence.integrity import open_state_db


def _populated(path: Path) -> None:
    conn = connect(path)
    migrate(conn)
    for i in range(400):
        conn.execute(
            "INSERT INTO incidents(ts, kind, severity, details_json) VALUES (?,?,?,?)",
            (float(i), "k", "info", "x" * 200),
        )
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()


def test_fresh_db_is_created(tmp_path: Path) -> None:
    r = open_state_db(tmp_path / "state.db", FakeClock())
    assert (r.safe_mode, r.reason) == (False, None)
    assert state.is_halted(r.conn) is False


def test_healthy_db_reopens_without_safe_mode(tmp_path: Path) -> None:
    _populated(tmp_path / "state.db")
    r = open_state_db(tmp_path / "state.db", FakeClock())
    assert r.safe_mode is False
    assert r.conn.execute("SELECT count(*) FROM incidents").fetchone()[0] == 400


def test_corrupt_db_goes_to_safe_mode(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _populated(path)
    with path.open("r+b") as f:
        f.seek(4096 * 6)
        f.write(b"\xff" * 4096)
    clock = FakeClock()
    r = open_state_db(path, clock)
    assert r.safe_mode is True and r.reason == "db_corrupt"
    assert state.is_halted(r.conn)
    assert state.get(r.conn, "halt_reason") == "db_corrupt"
    assert state.get(r.conn, "safe_mode") == "1"
    inc = r.conn.execute("SELECT kind, severity FROM incidents").fetchall()
    assert [(i["kind"], i["severity"]) for i in inc] == [("db_corrupt", "critical")]
    assert list(tmp_path.glob("state.db.corrupt-*"))
    # the next start keeps safe mode until resume
    r.conn.close()
    again = open_state_db(path, clock)
    assert again.safe_mode is True and again.reason == "db_corrupt"


def test_garbage_file_goes_to_safe_mode(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    path.write_bytes(b"this is not a database" * 100)
    r = open_state_db(path, FakeClock())
    assert r.safe_mode is True
    assert state.is_halted(r.conn)


def test_resume_after_corruption_needs_reconcile(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    path.write_bytes(b"garbage" * 1000)
    clock = FakeClock()
    r = open_state_db(path, clock)
    with pytest.raises(state.ReconcileRequired):
        state.unhalt(r.conn, clock)
    state.mark_reconciled(r.conn, clock)
    state.unhalt(r.conn, clock)
    assert not state.is_halted(r.conn)
    assert state.get(r.conn, "safe_mode") == "0"


def test_plain_halt_unhalt(tmp_path: Path) -> None:
    r = open_state_db(tmp_path / "state.db", FakeClock())
    clock = FakeClock()
    state.halt(r.conn, "kill", clock)
    assert state.is_halted(r.conn)
    state.unhalt(r.conn, clock)
    assert not state.is_halted(r.conn)
    state.heartbeat(r.conn, clock)
    assert float(state.get(r.conn, "heartbeat") or 0) == clock.now()


def test_backup_copies_and_keeps_seven(tmp_path: Path) -> None:
    r = open_state_db(tmp_path / "state.db", FakeClock())
    clock = FakeClock()
    r.conn.execute("INSERT INTO incidents(ts, kind, severity) VALUES (1,'k','info')")
    dst = tmp_path / "backups"
    paths = []
    for _ in range(9):
        paths.append(backup(r.conn, dst, clock))
        clock.advance_to(clock.now() + 1)
    left = sorted(dst.glob("state-*.db"))
    assert len(left) == 7 and left == sorted(paths[2:])
    copy = sqlite3.connect(str(left[-1]))
    assert copy.execute("SELECT count(*) FROM incidents").fetchone()[0] == 1


def test_daily_backup_once_per_day(tmp_path: Path) -> None:
    r = open_state_db(tmp_path / "state.db", FakeClock())
    clock = FakeClock()
    dst = tmp_path / "b"
    assert maybe_daily_backup(r.conn, dst, clock) is not None
    clock.advance_to(clock.now() + 3600)
    assert maybe_daily_backup(r.conn, dst, clock) is None
    clock.advance_to(clock.now() + 86400)
    assert maybe_daily_backup(r.conn, dst, clock) is not None
    assert len(list(dst.glob("state-*.db"))) == 2
