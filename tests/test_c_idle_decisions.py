"""E9/E12/E13/E14/E16 decisions: settle_idle, idle_stop_reason, drain, hold, leases."""

from llmhub.lifecycle_manager import leases, machine
from test_c_support import POOL, T0, add_instance, add_job, inst, make_cfg, make_conn, trail

CFG = make_cfg()  # idle_timeout 300 s


def test_ready_with_nothing_to_do_goes_idle_e9() -> None:
    conn = make_conn()
    add_instance(conn, "a", "READY")
    assert machine.settle_idle(conn, "a", T0 + 10, CFG) == "idle"
    assert inst(conn, "a")["idle_since"] == T0 + 10
    assert trail(conn) == [("READY", "IDLE", "E9")]


def test_busy_without_work_goes_idle_e12() -> None:
    conn = make_conn()
    add_instance(conn, "a", "BUSY")
    assert machine.settle_idle(conn, "a", T0 + 10, CFG) == "idle"
    assert trail(conn) == [("BUSY", "IDLE", "E12")]


def test_work_keeps_instance_active() -> None:
    for kind in ("queued", "running"):
        conn = make_conn()
        add_instance(conn, "a", "BUSY")
        add_job(conn, "j1", kind, instance_id="a" if kind == "running" else None)
        assert machine.settle_idle(conn, "a", T0, CFG) == "active"
        assert inst(conn, "a")["state"] == "BUSY"


def test_live_lease_keeps_instance_active_without_any_traffic() -> None:
    conn = make_conn()
    add_instance(conn, "a", "READY")
    lid = leases.create_explicit(conn, holder="agent", client_id=None, pool=POOL, now=T0)
    t = T0
    for _ in range(6):  # the agent is silent for 20 minutes but renews its lease
        t += 200
        leases.renew(conn, lid, t)
        assert machine.settle_idle(conn, "a", t, CFG) == "active"
    assert inst(conn, "a")["state"] == "READY"
    # heartbeat stops: lease expires after ttl, then the idle timer starts and runs out
    t += 601
    leases.expire_due(conn, t)
    assert machine.settle_idle(conn, "a", t, CFG) == "idle"
    assert machine.settle_idle(conn, "a", t + 299, CFG) == "idle"
    assert machine.settle_idle(conn, "a", t + 300, CFG) == "stopping"
    assert inst(conn, "a")["stop_reason"] == "idle_timeout"


def test_idle_timeout_boundaries_and_pool_setting() -> None:
    conn = make_conn()
    add_instance(conn, "a", "IDLE", idle_since=T0)
    row = inst(conn, "a")
    assert machine.idle_stop_reason(conn, row, T0 + 299, CFG) is None
    assert machine.idle_stop_reason(conn, row, T0 + 300, CFG) == "idle_timeout"
    assert machine.idle_due_at(row, CFG) == T0 + 300
    cfg0 = CFG.model_copy(deep=True)
    cfg0.pools[POOL].idle_timeout_s = 0
    assert machine.idle_stop_reason(conn, row, T0, cfg0) == "idle_timeout"
    cfg15 = CFG.model_copy(deep=True)
    cfg15.pools[POOL].idle_timeout_s = 900
    assert machine.idle_stop_reason(conn, row, T0 + 899, cfg15) is None


def test_reason_priority_and_stage_no_launch() -> None:
    conn = make_conn()
    add_instance(conn, "a", "IDLE", idle_since=T0, max_lifetime_at=T0 + 50)
    row = inst(conn, "a")
    assert machine.idle_stop_reason(conn, row, T0 + 10, CFG, no_launch=True) == "stage_no_launch"
    assert machine.idle_stop_reason(conn, row, T0 + 50, CFG, no_launch=True) == "max_lifetime"
    assert machine.idle_due_at(row, CFG) == T0 + 50


def test_drain_after_batch_stops_without_waiting_for_idle_timeout() -> None:
    conn = make_conn()
    add_instance(conn, "a", "BUSY")
    lid = leases.create_explicit(conn, holder="batch", client_id=None, pool=POOL, now=T0)
    assert machine.settle_idle(conn, "a", T0 + 10, CFG) == "active"
    leases.close(conn, lid, T0 + 20, final=True, reason="batch_done")
    assert machine.settle_idle(conn, "a", T0 + 20, CFG) == "stopping"  # E12 then E14 in one call
    assert [t[2] for t in trail(conn)] == ["E12", "drain"]


def test_drain_is_blocked_by_queue_or_other_lease() -> None:
    conn = make_conn()
    add_instance(conn, "a", "IDLE", idle_since=T0)
    lid = leases.create_explicit(conn, holder="x", client_id=None, pool=POOL, now=T0)
    leases.create_explicit(conn, holder="y", client_id=None, pool=POOL, now=T0)
    leases.close(conn, lid, T0 + 5, final=True)
    assert machine.settle_idle(conn, "a", T0 + 5, CFG) == "active"  # y still holds a lease
    assert inst(conn, "a")["state"] == "BUSY" and trail(conn)[-1][2] == "E13"


def test_hold_expiry_stops_at_once_hold_alive_blocks() -> None:
    conn = make_conn()
    add_instance(conn, "a", "READY")
    leases.create_hold(conn, holder="admin", pool=POOL, ttl_s=1000, now=T0)
    assert machine.settle_idle(conn, "a", T0 + 500, CFG) == "active"
    leases.expire_due(conn, T0 + 1000)
    assert machine.settle_idle(conn, "a", T0 + 1000, CFG) == "stopping"
    assert trail(conn)[-1][2] == "hold_expired"


def test_stage_no_launch_stops_idle() -> None:
    conn = make_conn()
    add_instance(conn, "a", "READY")
    assert machine.settle_idle(conn, "a", T0, CFG, no_launch=True) == "stopping"
    assert trail(conn)[-1][2] == "stage_no_launch"


def test_settle_idle_noop_cases() -> None:
    conn = make_conn()
    add_instance(conn, "a", "BOOTING")
    assert machine.settle_idle(conn, "a", T0, CFG) == "noop"
    assert machine.settle_idle(conn, "nope", T0, CFG) == "noop"
