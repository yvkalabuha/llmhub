"""T-368 review round 2: migration 0002 on an existing (v1) DB while controller and reaper
start together (deploy). Before the fix the loser of the race failed with "duplicate column
name" and the controller's `open_state_db` quarantined the healthy DB (HALTED, safe mode)."""

from __future__ import annotations

import threading
from pathlib import Path

from f_fixtures import cfg_with
from llmhub.clock import FakeClock
from llmhub.persistence.db import MIGRATIONS_DIR, connect, migrate
from llmhub.persistence.integrity import open_state_db
from llmhub.reaper.core import open_reaper_db


def _make_v1(path: Path) -> None:
    c = connect(path)
    script = (MIGRATIONS_DIR / "0001_initial.sql").read_text(encoding="utf-8")
    c.executescript(f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version=1;\nCOMMIT;")
    c.execute(
        "INSERT INTO instances(id, op_id, pool, provider, label, state, state_since,"
        " created_at, max_lifetime_at, accrued_to) VALUES"
        " ('inst-1','op1','p1','mock','llmhub-op1-00000000','STOPPED',0,0,0,0)"
    )
    c.close()


def _migrate(path: Path, busy_ms: int) -> None:
    conn = connect(path, busy_timeout_ms=busy_ms)
    try:
        migrate(conn)
    finally:
        conn.close()


def _race(path: Path, *fns: object) -> list[BaseException]:
    errors: list[BaseException] = []
    barrier = threading.Barrier(len(fns))

    def run(fn: object) -> None:
        barrier.wait()
        try:
            fn()  # type: ignore[operator]
        except BaseException as exc:  # noqa: BLE001 - collected for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(fn,)) for fn in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_concurrent_migrate_of_v1_db_both_succeed(tmp_path: Path) -> None:
    for i in range(20):
        path = tmp_path / f"s{i}.db"
        _make_v1(path)
        assert _race(path, lambda p=path: _migrate(p, 300), lambda p=path: _migrate(p, 5000)) == []
        a = connect(path)
        assert int(a.execute("PRAGMA user_version").fetchone()[0]) == 4
        row = a.execute("SELECT create_unknown, first_create_called_at FROM instances").fetchone()
        assert tuple(row) == (0, None)  # existing rows get the defaults
        a.close()


def test_controller_open_racing_reaper_does_not_quarantine(tmp_path: Path) -> None:
    cfg = cfg_with(tmp_path)
    for i in range(10):
        path = tmp_path / f"c{i}" / "state.db"
        path.parent.mkdir()
        _make_v1(path)
        results: list[tuple[bool, str | None]] = []

        def controller(p: Path = path, out: list[tuple[bool, str | None]] = results) -> None:
            res = open_state_db(p, FakeClock())
            out.append((res.safe_mode, res.reason))
            res.conn.close()

        errors = _race(path, controller, lambda p=path: open_reaper_db(p, cfg).close())
        assert errors == []
        assert results == [(False, None)]  # no HALT, no safe mode
        assert not list(path.parent.glob("state.db.corrupt-*"))
