"""T-371 A2: `backup_state_db` - consistent copy under a live WAL writer, rotation, modes."""

from __future__ import annotations

import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from llmhub.persistence.backup import backup_state_db
from llmhub.persistence.db import connect, migrate

NOW = datetime(2026, 11, 4, 3, 0, 0, tzinfo=UTC)


def _db(path: Path, rows: int = 50) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    for i in range(rows):
        conn.execute(
            "INSERT INTO incidents(ts, kind, severity, details_json) VALUES (?,?,?,?)",
            (float(i), "k", "info", "x" * 100),
        )
    return conn


def _count(path: Path) -> int:
    c = sqlite3.connect(str(path))
    try:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        return int(c.execute("SELECT count(*) FROM incidents").fetchone()[0])
    finally:
        c.close()


def test_copy_with_open_writer_matches(tmp_path: Path) -> None:
    src = tmp_path / "state.db"
    writer = _db(src)  # stays open; WAL not checkpointed
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO incidents(ts, kind, severity, details_json) VALUES (9,'u','info','')")
    try:
        out = backup_state_db(src, tmp_path / "backup", keep=14, now=NOW)
    finally:
        writer.execute("ROLLBACK")
    assert _count(out) == 50  # committed data only
    assert out.name.startswith("state-20261104T030000") and out.suffix == ".db"
    assert stat.S_IMODE(out.stat().st_mode) == 0o640
    assert not list((tmp_path / "backup").glob("*.tmp"))
    writer.close()


def test_same_second_does_not_overwrite(tmp_path: Path) -> None:
    src = tmp_path / "state.db"
    _db(src).close()
    a = backup_state_db(src, tmp_path / "b", keep=14, now=NOW)
    b = backup_state_db(src, tmp_path / "b", keep=14, now=NOW)
    assert a != b and a.exists() and b.exists()


def test_rotation_keeps_newest(tmp_path: Path) -> None:
    src = tmp_path / "state.db"
    _db(src).close()
    made = [
        backup_state_db(src, tmp_path / "b", keep=3, now=NOW.replace(day=d)) for d in range(1, 7)
    ]
    left = sorted(p.name for p in (tmp_path / "b").glob("state-*.db"))
    assert left == sorted(p.name for p in made[-3:])


def test_foreign_files_untouched(tmp_path: Path) -> None:
    src = tmp_path / "state.db"
    _db(src).close()
    d = tmp_path / "b"
    d.mkdir()
    (d / "notes.txt").write_text("keep")
    backup_state_db(src, d, keep=1, now=NOW)
    backup_state_db(src, d, keep=1, now=NOW.replace(hour=4))
    assert (d / "notes.txt").read_text() == "keep"
    assert len(list(d.glob("state-*.db"))) == 1


def test_missing_source_is_an_error_and_not_created(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        backup_state_db(tmp_path / "nope.db", tmp_path / "b", keep=3, now=NOW)
    assert not (tmp_path / "nope.db").exists()


def test_keep_must_be_positive(tmp_path: Path) -> None:
    src = tmp_path / "state.db"
    _db(src).close()
    with pytest.raises(ValueError):
        backup_state_db(src, tmp_path / "b", keep=0, now=NOW)
