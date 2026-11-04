"""C2: compare-and-set transitions, insert_provisioning, set_provider_ref."""

import sqlite3
import threading
from pathlib import Path

import pytest

from llmhub.budget_manager.limits import Gate
from llmhub.lifecycle_manager import machine
from llmhub.lifecycle_manager.states import IllegalTransition, State
from llmhub.persistence.db import connect
from llmhub.provider_adapters.base import UnavailableReason
from test_c_support import POOL, T0, add_instance, add_job, inst, make_conn, trail

OK = lambda c: Gate(ok=True)


def _insert(conn: sqlite3.Connection, op: str = "aaaaaaaaaaaa", gate=OK) -> str | None:  # type: ignore[no-untyped-def]
    return machine.insert_provisioning(
        conn, pool=POOL, provider="mock", op_id=op, label=f"llmhub-{op}-00000000",
        max_lifetime_at=T0 + 10800, now=T0, gate=gate,
    )  # fmt: skip


def test_transition_writes_row_and_state() -> None:
    conn = make_conn()
    add_instance(conn, "a", "PROVISIONING")
    assert machine.transition(conn, "a", State.PROVISIONING, State.BOOTING, "E2", T0 + 5,
                              "controller", endpoint="mock://x")  # fmt: skip
    row = inst(conn, "a")
    assert row["state"] == "BOOTING" and row["state_since"] == T0 + 5
    assert row["endpoint"] == "mock://x"
    assert trail(conn, "a") == [("PROVISIONING", "BOOTING", "E2")]


def test_transition_loses_when_state_differs() -> None:
    conn = make_conn()
    add_instance(conn, "a", "READY")
    assert not machine.transition(conn, "a", State.BOOTING, State.LOADING_MODEL, "E4", T0,
                                  "controller")  # fmt: skip
    assert trail(conn) == []


def test_illegal_edge_raises_and_unknown_field_rejected() -> None:
    conn = make_conn()
    add_instance(conn, "a", "READY")
    with pytest.raises(IllegalTransition):
        machine.transition(conn, "a", State.READY, State.STOPPED, "E20", T0, "controller")
    with pytest.raises(ValueError, match="unknown instance fields"):
        machine.transition(conn, "a", State.READY, State.IDLE, "E9", T0, "controller", bogus=1)


def test_e11_busy_to_busy_has_no_transition_row_and_keeps_state_since() -> None:
    conn = make_conn()
    add_instance(conn, "a", "BUSY", state_since=T0)
    assert machine.transition(conn, "a", State.BUSY, State.BUSY, "E11", T0 + 50, "controller")
    assert trail(conn) == []
    assert inst(conn, "a")["state_since"] == T0


def test_cas_two_connections_exactly_one_wins(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    c0 = make_conn(path)
    add_instance(c0, "a", "READY")
    wins: list[bool] = []
    barrier = threading.Barrier(2)

    def worker(actor: str) -> None:
        c = connect(path, busy_timeout_ms=5000)
        barrier.wait()
        wins.append(
            machine.transition(c, "a", State.READY, State.STOPPING, "E15", T0, actor)  # type: ignore[arg-type]
        )

    ts = [threading.Thread(target=worker, args=(a,)) for a in ("controller", "reaper")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(wins) == [False, True]
    assert len(trail(c0)) == 1
    assert c0.execute("SELECT actor FROM transitions").fetchone()[0] in ("controller", "reaper")


def test_transition_joins_open_transaction_and_rolls_back_with_it() -> None:
    from llmhub.persistence.db import tx

    conn = make_conn()
    add_instance(conn, "a", "READY")
    with pytest.raises(RuntimeError), tx(conn):
        machine.transition(conn, "a", State.READY, State.IDLE, "E9", T0, "controller")
        raise RuntimeError("abort")
    assert inst(conn, "a")["state"] == "READY" and trail(conn) == []


def test_insert_provisioning_ok_and_live_row_blocks() -> None:
    conn = make_conn()
    iid = _insert(conn)
    assert iid == "inst-aaaaaaaaaaaa"
    assert trail(conn) == [("STOPPED", "PROVISIONING", "E1")]
    assert inst(conn, iid)["accrued_to"] == T0
    assert _insert(conn, "bbbbbbbbbbbb") is None  # live row of the pool
    assert conn.execute("SELECT count(*) FROM instances").fetchone()[0] == 1


@pytest.mark.parametrize("old", ["PENDING_DELETE", "LEAKED", "STOPPED"])
def test_insert_provisioning_new_cycle_beside_dying_row(old: str) -> None:
    conn = make_conn()
    add_instance(conn, "old", old)
    assert _insert(conn) is not None


def test_insert_provisioning_gate_failure_inserts_nothing() -> None:
    conn = make_conn()
    seen: list[bool] = []

    def gate(c: sqlite3.Connection) -> Gate:
        seen.append(c.in_transaction)  # the gate runs inside the insert transaction
        return Gate(ok=False, reason=UnavailableReason.BUDGET_LIMIT_REACHED)

    assert _insert(conn, gate=gate) is None
    assert seen == [True]
    assert conn.execute("SELECT count(*) FROM instances").fetchone()[0] == 0
    assert trail(conn) == []


@pytest.mark.parametrize("state", ["PROVISIONING", "STOPPING", "ERROR", "PENDING_DELETE"])
def test_set_provider_ref_is_unconditional_and_returns_state(state: str) -> None:
    conn = make_conn()
    add_instance(conn, "a", state)
    got = machine.set_provider_ref(conn, "a", "ref-1", "host-9", 0.31, T0)
    assert got == State(state)
    row = inst(conn, "a")
    assert (row["provider_ref"], row["host_id"], row["price_usd_h"]) == ("ref-1", "host-9", 0.31)
    assert row["state"] == state  # no state change, no CAS


def test_live_instance_ignores_dying_rows() -> None:
    conn = make_conn()
    add_instance(conn, "old", "PENDING_DELETE")
    assert machine.live_instance(conn, POOL) is None
    add_instance(conn, "new", "BOOTING")
    row = machine.live_instance(conn, POOL)
    assert row is not None and row["id"] == "new"


def test_requeue_running_jobs_counts_or_not() -> None:
    conn = make_conn()
    add_instance(conn, "a", "BUSY")
    add_job(conn, "j1", "running", instance_id="a", worker_id="w", attempts=1)
    add_job(conn, "j2", "running", instance_id="a", worker_id="w", attempts=2)
    ids = machine.requeue_running_jobs(conn, T0, instance_id="a", count_attempt=True)
    assert ids == ["j1"]  # j2 hit max_attempts
    j1 = conn.execute("SELECT * FROM jobs WHERE id='j1'").fetchone()
    j2 = conn.execute("SELECT * FROM jobs WHERE id='j2'").fetchone()
    assert (j1["status"], j1["attempts"], j1["instance_id"]) == ("queued", 1, None)
    assert (j2["status"], j2["error_code"]) == ("failed", "max_attempts")
    assert conn.execute("SELECT closed_at FROM leases WHERE job_id='j2'").fetchone()[0] == T0
    assert conn.execute("SELECT closed_at FROM leases WHERE job_id='j1'").fetchone()[0] is None
    # restart path: attempt is given back, restarts counted
    add_job(conn, "j3", "running", instance_id="a", worker_id="old-boot", attempts=1)
    machine.requeue_running_jobs(conn, T0, not_worker="boot-2", count_attempt=False)
    j3 = conn.execute("SELECT * FROM jobs WHERE id='j3'").fetchone()
    assert (j3["status"], j3["attempts"], j3["restarts"]) == ("queued", 0, 1)
