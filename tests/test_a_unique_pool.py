import sqlite3
import threading
from pathlib import Path

import pytest

from llmhub.persistence.db import connect, migrate


def _insert(conn: sqlite3.Connection, n: str, pool: str, state: str) -> None:
    conn.execute(
        "INSERT INTO instances(id, op_id, pool, provider, label, state, state_since,"
        " created_at, max_lifetime_at) VALUES (?,?,?,?,?,?,0,0,0)",
        (n, "op-" + n, pool, "mock", "llmhub-" + n, state),
    )


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def test_second_live_row_rejected(conn: sqlite3.Connection) -> None:
    _insert(conn, "a", "p", "READY")
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, "b", "p", "PROVISIONING")


def test_other_pool_allowed(conn: sqlite3.Connection) -> None:
    _insert(conn, "a", "p", "READY")
    _insert(conn, "b", "q", "READY")


@pytest.mark.parametrize("state", ["PENDING_DELETE", "LEAKED", "STOPPED"])
def test_non_live_states_allow_new_cycle(conn: sqlite3.Connection, state: str) -> None:
    _insert(conn, "a", "p", state)
    _insert(conn, "b", "p", "PROVISIONING")
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, "c", "p", "BOOTING")


@pytest.mark.parametrize("state", ["STOPPING", "ERROR", "IDLE", "BUSY"])
def test_stopping_and_error_are_live(conn: sqlite3.Connection, state: str) -> None:
    _insert(conn, "a", "p", state)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, "b", "p", "PROVISIONING")


def test_ten_threads_exactly_one_insert(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    migrate(connect(path))
    results: list[bool] = []
    barrier = threading.Barrier(10)

    def worker(i: int) -> None:
        c = connect(path, busy_timeout_ms=5000)
        barrier.wait()
        try:
            c.execute("BEGIN IMMEDIATE")
            _insert(c, f"t{i}", "p", "PROVISIONING")
            c.execute("COMMIT")
            results.append(True)
        except sqlite3.IntegrityError:
            c.execute("ROLLBACK")
            results.append(False)
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False] * 9 + [True]
    assert connect(path).execute("SELECT count(*) FROM instances").fetchone()[0] == 1
