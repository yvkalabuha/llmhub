"""C4: IDLE -> STOPPING decision in one transaction with the queue and leases (E13/E14)."""

import threading
from pathlib import Path

from llmhub.lifecycle_manager import leases, machine
from llmhub.persistence.db import connect
from test_c_support import POOL, T0, add_instance, add_job, inst, make_conn, trail


def _idle(conn):  # type: ignore[no-untyped-def]
    return add_instance(conn, "a", "IDLE", idle_since=T0)


def test_empty_queue_and_no_leases_stops() -> None:
    conn = make_conn()
    _idle(conn)
    assert machine.try_idle_to_stopping(conn, "a", T0 + 300, "idle_timeout") == "stopping"
    row = inst(conn, "a")
    assert row["state"] == "STOPPING" and row["stop_reason"] == "idle_timeout"
    assert trail(conn) == [("IDLE", "STOPPING", "idle_timeout")]


def test_queued_job_wins_goes_busy() -> None:
    conn = make_conn()
    _idle(conn)
    add_job(conn, "j1", "queued")
    assert machine.try_idle_to_stopping(conn, "a", T0 + 300, "idle_timeout") == "busy"
    row = inst(conn, "a")
    assert row["state"] == "BUSY" and row["idle_since"] is None
    assert trail(conn) == [("IDLE", "BUSY", "E13")]


def test_job_in_other_pool_does_not_block() -> None:
    conn = make_conn()
    _idle(conn)
    add_job(conn, "j1", "queued", pool="other")
    assert machine.try_idle_to_stopping(conn, "a", T0, "idle_timeout") == "stopping"


def test_live_explicit_lease_wins() -> None:
    conn = make_conn()
    _idle(conn)
    leases.create_explicit(conn, holder="agent", client_id=None, pool=POOL, now=T0)
    assert machine.try_idle_to_stopping(conn, "a", T0 + 100, "idle_timeout") == "busy"


def test_expired_lease_does_not_block() -> None:
    conn = make_conn()
    _idle(conn)
    leases.create_explicit(conn, holder="agent", client_id=None, pool=POOL, ttl_s=60, now=T0)
    assert machine.try_idle_to_stopping(conn, "a", T0 + 61, "idle_timeout") == "stopping"


def test_not_idle_or_missing_is_noop() -> None:
    conn = make_conn()
    add_instance(conn, "a", "READY")
    assert machine.try_idle_to_stopping(conn, "a", T0, "x") == "noop"
    assert machine.try_idle_to_stopping(conn, "zzz", T0, "x") == "noop"
    assert trail(conn) == []


def test_race_no_stopping_with_nonempty_queue(tmp_path: Path) -> None:
    """200 rounds: a writer inserts a job while another connection decides to stop.

    A trigger aborts any IDLE -> STOPPING write that happens while a queued job exists, so a
    violating interleaving fails loudly instead of depending on timing.
    """
    path = tmp_path / "race.db"
    c0 = make_conn(path)
    c0.execute(
        "CREATE TRIGGER no_stop_with_queue BEFORE UPDATE OF state ON instances"
        " WHEN NEW.state='STOPPING' AND EXISTS ("
        "   SELECT 1 FROM jobs WHERE pool=NEW.pool AND status='queued')"
        " BEGIN SELECT RAISE(ABORT, 'STOPPING with a queued job'); END"
    )
    add_instance(c0, "a", "IDLE", idle_since=T0)
    outcomes: list[str] = []
    errors: list[BaseException] = []
    for i in range(200):
        c0.execute("DELETE FROM leases")
        c0.execute("DELETE FROM jobs")
        c0.execute("UPDATE instances SET state='IDLE', stop_reason=NULL, idle_since=?", (T0,))
        barrier = threading.Barrier(2)

        def writer(n: int = i, barrier: threading.Barrier = barrier) -> None:
            try:
                c = connect(path, busy_timeout_ms=5000)
                barrier.wait()
                add_job_tx(c, f"j{n}")
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        def decider(barrier: threading.Barrier = barrier) -> None:
            try:
                c = connect(path, busy_timeout_ms=5000)
                barrier.wait()
                outcomes.append(machine.try_idle_to_stopping(c, "a", T0 + 300, "idle_timeout"))
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        ts = [threading.Thread(target=writer), threading.Thread(target=decider)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert not errors, errors
        state = inst(c0, "a")["state"]
        assert state in ("STOPPING", "BUSY")
    assert set(outcomes) == {"stopping", "busy"} or set(outcomes) <= {"stopping", "busy"}


def add_job_tx(conn, jid: str) -> None:  # type: ignore[no-untyped-def]
    conn.execute("BEGIN IMMEDIATE")
    try:
        add_job(conn, jid, "queued")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
