import threading
import time
from pathlib import Path

import pytest

from llmhub.clock import FakeClock
from llmhub.persistence.db import Busy, assert_not_in_tx, connect, migrate, tx


def _db(tmp_path: Path, ms: int = 300):
    path = tmp_path / "state.db"
    migrate(connect(path))
    return path, connect(path, busy_timeout_ms=ms)


def _set(conn, key: str, val: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO controller_state(key,value,updated_at) VALUES (?,?,0)", (key, val)
    )


def test_tx_commits(tmp_path: Path) -> None:
    path, conn = _db(tmp_path)
    with tx(conn):
        _set(conn, "k", "v")
    other = connect(path)
    assert other.execute("SELECT value FROM controller_state WHERE key='k'").fetchone()[0] == "v"


def test_tx_rolls_back_on_exception(tmp_path: Path) -> None:
    _, conn = _db(tmp_path)
    with pytest.raises(RuntimeError), tx(conn):
        _set(conn, "k", "v")
        raise RuntimeError("boom")
    assert conn.execute("SELECT count(*) FROM controller_state WHERE key='k'").fetchone()[0] == 0
    assert not conn.in_transaction


def test_assert_not_in_tx(tmp_path: Path) -> None:
    _, conn = _db(tmp_path)
    assert_not_in_tx()
    with tx(conn), pytest.raises(AssertionError):
        assert_not_in_tx()
    assert_not_in_tx()


def test_nested_tx_rejected(tmp_path: Path) -> None:
    _, conn = _db(tmp_path)
    with tx(conn), pytest.raises(AssertionError), tx(conn):
        pass


async def test_clock_sleep_inside_tx_asserts(tmp_path: Path) -> None:
    _, conn = _db(tmp_path)
    clock = FakeClock()
    with tx(conn), pytest.raises(AssertionError):
        await clock.sleep_until(clock.now() + 5)


def _hold_write(path: Path, started: threading.Event, release: threading.Event) -> None:
    c = connect(path, busy_timeout_ms=5000)
    c.execute("BEGIN IMMEDIATE")
    started.set()
    release.wait(10)
    c.execute("COMMIT")
    c.close()


def test_contention_controller_gets_busy(tmp_path: Path) -> None:
    path, conn = _db(tmp_path, ms=300)
    started, release = threading.Event(), threading.Event()
    t = threading.Thread(target=_hold_write, args=(path, started, release))
    t.start()
    started.wait(5)
    t0 = time.monotonic()
    with pytest.raises(Busy), tx(conn):
        _set(conn, "k", "v")
    assert 0.2 < time.monotonic() - t0 < 2.0
    assert not conn.in_transaction
    release.set()
    t.join()
    with tx(conn):  # the action is retried on the next step and succeeds
        _set(conn, "k", "v")


def test_contention_reaper_waits(tmp_path: Path) -> None:
    path, _ = _db(tmp_path)
    reaper = connect(path, busy_timeout_ms=5000)
    started, release = threading.Event(), threading.Event()
    t = threading.Thread(target=_hold_write, args=(path, started, release))
    t.start()
    started.wait(5)
    threading.Timer(0.7, release.set).start()
    t0 = time.monotonic()
    with tx(reaper):
        _set(reaper, "k", "v")
    assert time.monotonic() - t0 >= 0.5
    t.join()
    assert reaper.execute("SELECT value FROM controller_state WHERE key='k'").fetchone()[0] == "v"
