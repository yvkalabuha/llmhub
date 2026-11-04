"""Review cycle 1 (logic and races) of T-368: reproductions of findings and race checks.

The finding reproductions (R1-x, see the task file, «Ревью (цикл 1)») were xfail until the fix
round 1 and now pass; the rest pin down properties the review verified (CAS races between
controller and reaper).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import test_c_support as C
import test_e_support as E
from harness import Harness, make_job
from llmhub.budget_manager.limits import committed, windows
from llmhub.budget_manager.reservations import release_ref, reserve
from llmhub.cost_tracker.ledger import accrue
from llmhub.lifecycle_manager import leases, machine, teardown
from llmhub.persistence.db import connect, migrate, tx
from llmhub.reaper import core as reaper_core


def _committed_day(conn, now: float) -> float:  # type: ignore[no-untyped-def]
    w = windows(now)
    return committed(conn, w.day_start, w.day_end, now)


# --- R1-1: FIFO "session first" + release of the job reservation under-counts committed ------
def test_job_reservation_release_keeps_session_cover(tmp_path: Path) -> None:
    conn = E.make_conn(tmp_path)
    E.add_instance(conn, "i1", state="BUSY", price=3.6, accrued_to=E.T0)  # r = 0.001 $/s
    with tx(conn):
        reserve(conn, "session", "op1", 1.0, E.T0, "i1")  # T_cold + launch jobs + T_idle
        reserve(conn, "job", "j2", 0.3, E.T0, "i1")  # a non-launch job on the live GPU
    assert _committed_day(conn, E.T0) == pytest.approx(1.3)
    # j2 runs 300 s (0.3 $) and completes; nothing of the session's own plan was used yet
    accrue(conn, E.T0 + 300)
    release_ref(conn, "job", "j2", E.T0 + 300, "job_done")
    # spent 0.3 + the whole session plan 1.0 is still ahead
    assert _committed_day(conn, E.T0 + 300) >= 1.3 - 1e-9


# --- R1-2: hold reservation without an instance is never consumed -------------------------------
def test_hold_reservation_without_instance_is_consumed(tmp_path: Path) -> None:
    conn = E.make_conn(tmp_path)
    E.add_instance(conn, "i1", state="BUSY", price=3.6, accrued_to=E.T0)
    with tx(conn):
        reserve(conn, "session", "op1", 0.1, E.T0, "i1")
        reserve(conn, "hold", "lease-h", 0.5, E.T0, None)  # Controller.hold with no live GPU
    accrue(conn, E.T0 + 400)  # 0.4 $ spent: 0.1 session + 0.3 under the hold
    # spent 0.4 + hold left 0.2 = 0.6 (implementation: 0.4 + 0.5 = 0.9)
    assert _committed_day(conn, E.T0 + 400) == pytest.approx(0.6)


# --- R1-3: drain is lost when the final close precedes the end of the last job -------------------
def test_drain_when_final_close_precedes_last_job_end() -> None:
    conn = C.make_conn()
    C.add_instance(conn, "a", "BUSY")
    C.add_job(conn, "j1", "running", instance_id="a", worker_id="w")
    lid = leases.create_explicit(conn, holder="agent", client_id=None, pool=C.POOL, now=C.T0)
    leases.close(conn, lid, C.T0 + 20, final=True, reason="batch_done")  # batch submitted
    assert machine.settle_idle(conn, "a", C.T0 + 20, C.make_cfg()) == "active"  # j1 runs
    with tx(conn):  # j1 completes at T0+50
        conn.execute("UPDATE jobs SET status='completed', finished_at=? WHERE id='j1'",
                     (C.T0 + 50,))  # fmt: skip
        leases.close_for_job(conn, "j1", C.T0 + 50)
    assert machine.settle_idle(conn, "a", C.T0 + 50, C.make_cfg()) == "stopping"


# --- R1-4: reaper-confirmed E20 skips allocate_session ------------------------------------------
async def test_reaper_e20_allocates_session() -> None:
    conn = C.make_conn()
    cloud = C.FakeCloud()
    C.add_instance(conn, "a", "PENDING_DELETE", provider_ref="gone", price_usd_h=0.36,
                   accrued_to=C.T0, destroy_requested_at=C.T0)  # fmt: skip
    row = C.inst(conn, "a")
    ok = await teardown.confirm_zero(conn, cloud, row, C.T0 + 100, C.make_cfg(), "reaper",
                                     on_stopped=reaper_core._on_stopped)  # fmt: skip
    assert ok
    n = conn.execute("SELECT count(*) FROM cost_allocations WHERE instance_id='a'").fetchone()[0]
    assert n > 0


# --- R1-5: a cleared create_called_at lets zero be confirmed without the visibility grace ----------
async def test_no_ref_row_waits_grace_after_an_earlier_create() -> None:
    conn = C.make_conn()
    cloud = C.FakeCloud()
    # attempt 1 had an unknown outcome (attempts=1); the retry path cleared create_called_at
    # and found no offer; then kill -> STOPPING -> PENDING_DELETE
    C.add_instance(conn, "a", "PENDING_DELETE", attempts=1, last_error="timeout",
                   destroy_requested_at=C.T0 + 130)  # fmt: skip
    cloud.add("llmhub-a", hidden=True)  # created by attempt 1, not yet listed (lag)
    row = C.inst(conn, "a")
    ok = await teardown.confirm_zero(conn, cloud, row, C.T0 + 131, C.make_cfg(), "controller")
    assert not ok


# --- R1-6: launch-set job re-run on the same instance has no reservation/check ---------------------
async def test_launch_job_rerun_is_checked(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    jid = h.submit(make_job(0)).job_id
    for _ in range(400):
        await h.step()
        h.clock.advance_to(h.clock.now() + 1)
        if h.state_of() == "BUSY":
            break
    assert h.state_of() == "BUSY"
    inst = h.instances()[-1]
    h.inference.lose_network(inst["provider_ref"], h.clock.now() + 60)
    first_start = h.jobs()[0]["started_at"]
    await h.run_for(600)
    job = h.conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    assert job["status"] == "completed" and job["started_at"] > first_start  # it ran twice
    n = h.conn.execute(
        "SELECT count(*) FROM reservations WHERE scope='job' AND ref_id=?", (jid,)
    ).fetchone()[0]
    assert n >= 1


# --- R1-7: launch estimate uses the previous instance price, not the cap ---------------------------
def test_launch_price_not_below_possible_offer(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.conn.execute(
        "INSERT INTO instances(id, op_id, pool, provider, label, state, state_since, created_at,"
        " max_lifetime_at, price_usd_h) VALUES ('old','old','p1','mock','llmhub-old','STOPPED',"
        " 0, 0, 0, 0.10)"
    )
    price, _ = h.controller._pool_price("p1")
    assert price >= h.cfg.pools["p1"].max_price_usd_h


# --- verified races (must pass) ------------------------------------------------------------------
async def test_controller_and_reaper_confirm_zero_once(tmp_path: Path) -> None:
    """Both processes confirm zero from the same stale snapshot: one E20, one finalize."""
    path = tmp_path / "state.db"
    a = connect(path)
    migrate(a)
    b = connect(path, busy_timeout_ms=5000)
    a.execute("INSERT INTO clients(id, name, key_hash, created_at) VALUES ('c1','c1','x',0)")
    C.add_instance(a, "x", "PENDING_DELETE", provider_ref="gone", price_usd_h=0.36,
                   accrued_to=C.T0, destroy_requested_at=C.T0)  # fmt: skip
    snap_a, snap_b = C.inst(a, "x"), C.inst(b, "x")
    calls: list[str] = []

    def hook(name: str):  # type: ignore[no-untyped-def]
        def f(conn, row, now) -> None:  # type: ignore[no-untyped-def]
            calls.append(name)
            reaper_core._on_stopped(conn, row, now)

        return f

    cloud = C.FakeCloud()
    cfg = C.make_cfg()
    r1, r2 = await asyncio.gather(
        teardown.confirm_zero(a, cloud, snap_a, C.T0 + 100, cfg, "controller", on_stopped=hook("c")),
        teardown.confirm_zero(b, cloud, snap_b, C.T0 + 101, cfg, "reaper", on_stopped=hook("r")),
    )
    assert sorted([r1, r2]) == [False, True]
    assert len(calls) == 1
    rows = a.execute("SELECT count(*) FROM transitions WHERE to_state='STOPPED'").fetchone()[0]
    assert rows == 1
    ledger_rows = a.execute("SELECT count(*) FROM ledger WHERE instance_id='x'").fetchone()[0]
    assert ledger_rows == 1


async def test_reaper_e19_and_controller_e19_race_single_transition(tmp_path: Path) -> None:
    """STOPPING row: controller and reaper both run request_destroy; one E19 wins by CAS."""
    path = tmp_path / "state.db"
    a = connect(path)
    migrate(a)
    b = connect(path, busy_timeout_ms=5000)
    a.execute("INSERT INTO clients(id, name, key_hash, created_at) VALUES ('c1','c1','x',0)")
    cloud = C.FakeCloud()
    res = cloud.add("llmhub-x")
    C.add_instance(a, "x", "STOPPING", provider_ref=res.ref)
    cfg = C.make_cfg()
    snap_a, snap_b = C.inst(a, "x"), C.inst(b, "x")
    await asyncio.gather(
        teardown.request_destroy(a, cloud, snap_a, C.T0 + 200, cfg, "controller"),
        teardown.request_destroy(b, cloud, snap_b, C.T0 + 200, cfg, "reaper"),
    )
    assert C.inst(a, "x")["state"] == "PENDING_DELETE"
    n = a.execute("SELECT count(*) FROM transitions WHERE to_state='PENDING_DELETE'").fetchone()[0]
    assert n == 1
    assert res.ref not in cloud.resources


def test_try_idle_to_stopping_sees_lease_from_other_connection(tmp_path: Path) -> None:
    """A lease committed by another connection between decision and tx keeps the GPU (E13)."""
    path = tmp_path / "state.db"
    a = connect(path)
    migrate(a)
    b = connect(path)
    a.execute("INSERT INTO clients(id, name, key_hash, created_at) VALUES ('c1','c1','x',0)")
    C.add_instance(a, "x", "IDLE", idle_since=C.T0)
    row = C.inst(a, "x")
    cfg = C.make_cfg()
    assert machine.idle_stop_reason(a, row, C.T0 + 400, cfg) == "idle_timeout"
    leases.create_explicit(b, holder="agent", client_id=None, pool=C.POOL, now=C.T0 + 400)
    assert machine.try_idle_to_stopping(a, "x", C.T0 + 400, "idle_timeout") == "busy"
    assert C.inst(a, "x")["state"] == "BUSY"


# --- R1-8: a hold placed before the launch does not extend the new instance's max_lifetime --------
def test_hold_extends_lifetime_of_instance_launched_later() -> None:
    from llmhub.budget_manager.limits import Gate

    conn = C.make_conn()
    cfg = C.make_cfg()
    hold_ttl = 6 * 3600.0  # > max_lifetime_s (3 h); allowed with hold_may_exceed_session
    leases.create_hold(conn, holder="admin", pool=C.POOL, ttl_s=hold_ttl, now=C.T0)
    iid = machine.insert_provisioning(
        conn, pool=C.POOL, provider="fake", op_id="op1", label="llmhub-op1",
        max_lifetime_at=C.T0 + 60 + cfg.timeouts.max_lifetime_s, now=C.T0 + 60,
        gate=lambda _c: Gate(ok=True),
    )  # fmt: skip
    assert iid is not None
    assert float(C.inst(conn, iid)["max_lifetime_at"]) >= C.T0 + hold_ttl
