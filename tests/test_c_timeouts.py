"""Edges E3/E5/E7/E10/E15/E16/E17/E18: timeouts, loss, kill, liveness, model-test failures."""

import json
import sqlite3

import pytest

from llmhub.lifecycle_manager import leases, machine, timeouts
from llmhub.provider_adapters.base import RemoteStatus
from test_c_support import (
    POOL,
    T0,
    FakeCloud,
    FakeInference,
    add_instance,
    add_job,
    inst,
    make_cfg,
    make_conn,
    trail,
)

CFG = make_cfg()


def _blacklist(conn: sqlite3.Connection) -> list[tuple[str, float]]:
    return [(r["host_id"], r["until"]) for r in conn.execute("SELECT * FROM host_blacklist")]


@pytest.mark.parametrize(
    ("state", "edge", "limit", "blacklisted"),
    [
        ("PROVISIONING", "E3", 600, False),
        ("BOOTING", "E5", 600, True),
        ("LOADING_MODEL", "E7", 1200, True),
    ],
)
def test_state_timeouts(state: str, edge: str, limit: int, blacklisted: bool) -> None:
    conn = make_conn()
    add_instance(conn, "a", state, host_id="h1", provider="mock")
    assert timeouts.apply_timeouts(conn, T0 + limit - 1, CFG) == []
    assert inst(conn, "a")["state"] == state
    assert timeouts.apply_timeouts(conn, T0 + limit, CFG) == [("a", edge)]
    row = inst(conn, "a")
    assert row["state"] == "ERROR" and row["last_error"] == f"{state.lower()}_timeout"
    assert _blacklist(conn) == ([("h1", T0 + limit + 86400)] if blacklisted else [])
    assert trail(conn)[-1][:2] == (state, "ERROR")


def test_ready_has_no_state_timeout() -> None:
    conn = make_conn()
    add_instance(conn, "a", "READY")
    assert timeouts.apply_timeouts(conn, T0 + 9000, CFG) == []


@pytest.mark.parametrize("state", ["PROVISIONING", "BOOTING", "LOADING_MODEL", "READY", "IDLE"])
def test_max_lifetime_stops_non_busy_via_e15_even_with_lease_and_queue(state: str) -> None:
    conn = make_conn()
    add_instance(conn, "a", state, max_lifetime_at=T0 + 100, idle_since=T0, state_since=T0 + 50)
    leases.create_explicit(conn, holder="a", client_id=None, pool=POOL, now=T0, ttl_s=3600)
    add_job(conn, "j1", "queued")
    assert timeouts.apply_timeouts(conn, T0 + 100, CFG) == [("a", "E15")]
    row = inst(conn, "a")
    assert row["state"] == "STOPPING" and row["stop_reason"] == "max_lifetime"
    assert conn.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()[0] == "queued"


def test_max_lifetime_busy_finishes_current_then_e16() -> None:
    conn = make_conn()
    add_instance(conn, "a", "BUSY", max_lifetime_at=T0 + 100)
    add_job(conn, "j1", "running", instance_id="a", worker_id="w", attempts=1)
    assert timeouts.apply_timeouts(conn, T0 + 100, CFG) == [("a", "stop_after_current")]
    assert inst(conn, "a")["state"] == "BUSY" and inst(conn, "a")["stop_after_current"] == 1
    assert timeouts.apply_timeouts(conn, T0 + 101, CFG) == []  # not repeated
    assert machine.settle_idle(conn, "a", T0 + 110, CFG) == "active"
    assert inst(conn, "a")["state"] == "BUSY"  # job still running
    conn.execute("UPDATE jobs SET status='completed' WHERE id='j1'")
    assert machine.settle_idle(conn, "a", T0 + 120, CFG) == "stopping"
    assert trail(conn, "a")[-1] == ("BUSY", "STOPPING", "E16")
    assert inst(conn, "a")["stop_reason"] == "max_lifetime"


def test_model_test_failures_three_in_window_to_error_with_blacklist() -> None:
    conn = make_conn()
    add_instance(conn, "a", "LOADING_MODEL", host_id="h2", provider="mock")
    assert not timeouts.note_model_test_failure(conn, "a", T0, CFG)
    assert not timeouts.note_model_test_failure(conn, "a", T0 + 20, CFG)
    assert timeouts.note_model_test_failure(conn, "a", T0 + 40, CFG)
    assert (
        inst(conn, "a")["state"] == "ERROR" and inst(conn, "a")["last_error"] == "model_test_failed"
    )
    assert [h for h, _ in _blacklist(conn)] == ["h2"]


def test_model_test_failures_outside_window_do_not_count() -> None:
    conn = make_conn()
    add_instance(conn, "a", "LOADING_MODEL")
    for dt in (0, 100, 200, 300):  # 60 s window
        assert not timeouts.note_model_test_failure(conn, "a", T0 + dt, CFG)
    assert len(json.loads(inst(conn, "a")["model_test_failures_json"])) == 1
    assert inst(conn, "a")["state"] == "LOADING_MODEL"
    # not LOADING_MODEL: ignored
    add_instance(conn, "b", "READY", pool="q")
    assert not timeouts.note_model_test_failure(conn, "b", T0, CFG)


@pytest.mark.parametrize(
    ("state", "edge", "attempts_after"),
    [("READY", "E10", None), ("BUSY", "E17", 1), ("IDLE", "E17", None),
     ("BOOTING", "E5", None), ("LOADING_MODEL", "E7", None), ("PROVISIONING", "E3", None)],
)  # fmt: skip
def test_mark_lost_uses_the_edge_of_the_state(
    state: str, edge: str, attempts_after: int | None
) -> None:
    conn = make_conn()
    add_instance(conn, "a", state, host_id="h3", provider="mock")
    if state == "BUSY":
        add_job(conn, "j1", "running", instance_id="a", worker_id="w", attempts=1)
    assert machine.mark_lost(conn, "a", T0 + 7, "resource_gone", cfg=CFG)
    assert inst(conn, "a")["state"] == "ERROR"
    assert trail(conn)[-1] == (state, "ERROR", "resource_gone")
    assert bool(_blacklist(conn)) == (state in ("BOOTING", "LOADING_MODEL"))
    if state == "BUSY":  # E17: the attempt is counted
        j = conn.execute("SELECT * FROM jobs WHERE id='j1'").fetchone()
        assert (j["status"], j["attempts"]) == ("queued", attempts_after)
    assert conn.execute("SELECT count(*) FROM incidents").fetchone()[0] == 1
    assert not machine.mark_lost(conn, "a", T0 + 8, "again", cfg=CFG)  # ERROR: no second edge


def test_mark_lost_busy_job_at_max_attempts_falls_back() -> None:
    conn = make_conn()
    add_instance(conn, "a", "BUSY")
    add_job(conn, "j1", "running", instance_id="a", worker_id="w", attempts=2)
    machine.mark_lost(conn, "a", T0, "interrupted", cfg=CFG)
    row = conn.execute("SELECT status, route FROM jobs").fetchone()
    assert (row["status"], row["route"]) == ("queued", "local:fallback")  # queue.requeue (F)


def test_error_to_stopping_e18() -> None:
    conn = make_conn()
    add_instance(conn, "a", "ERROR")
    add_instance(conn, "b", "READY", pool="q")
    assert machine.error_to_stopping(conn, T0 + 1) == ["a"]
    assert inst(conn, "a")["state"] == "STOPPING" and inst(conn, "b")["state"] == "READY"
    assert trail(conn)[-1] == ("ERROR", "STOPPING", "E18")


def test_kill_to_stopping_all_states_requeues_without_attempt_and_releases_hold() -> None:
    conn = make_conn()
    for i, s in enumerate(
        ["PROVISIONING", "BOOTING", "LOADING_MODEL", "READY", "BUSY", "IDLE", "ERROR"]
    ):
        add_instance(conn, f"i{i}", s, pool=f"p{i}")
    add_job(conn, "j1", "running", instance_id="i4", worker_id="w", attempts=1, pool="p4")
    leases.create_hold(conn, holder="admin", pool="p3", ttl_s=3600, now=T0)
    done = machine.kill_to_stopping(conn, T0 + 5)
    assert sorted(done) == [f"i{i}" for i in range(7)]
    assert {r["state"] for r in conn.execute("SELECT state FROM instances")} == {"STOPPING"}
    j = conn.execute("SELECT * FROM jobs").fetchone()
    assert (j["status"], j["attempts"]) == ("queued", 0)
    assert leases.live_count(conn, "p3", T0 + 6) == 0
    edges = [t[2] for t in trail(conn)]
    assert edges.count("kill") == 7
    assert machine.kill_to_stopping(conn, T0 + 6) == []  # STOPPING is left alone


# --- liveness ---------------------------------------------------------------------------------


async def _alive(
    conn: sqlite3.Connection, cloud: FakeCloud, inf: FakeInference, iid: str, now: float
) -> str:
    return await timeouts.check_alive(conn, cloud, inf, inst(conn, iid), now, CFG)


async def test_check_alive_ok_and_skipped() -> None:
    conn, cloud, inf = make_conn(), FakeCloud(), FakeInference()
    r = cloud.add("llmhub-a")
    add_instance(conn, "a", "READY", provider_ref=r.ref, endpoint="e")
    add_instance(conn, "p", "PROVISIONING", pool="q")
    assert await _alive(conn, cloud, inf, "a", T0) == "ok"
    assert await _alive(conn, cloud, inf, "p", T0) == "skipped"


@pytest.mark.parametrize("state", ["BOOTING", "LOADING_MODEL", "READY", "BUSY", "IDLE"])
async def test_check_alive_resource_gone(state: str) -> None:
    conn, cloud, inf = make_conn(), FakeCloud(), FakeInference()
    add_instance(conn, "a", state, provider_ref="r-gone", endpoint="e")
    assert await _alive(conn, cloud, inf, "a", T0) == "lost"
    assert inst(conn, "a")["state"] == "ERROR"


async def test_check_alive_terminated_and_stopped_but_billing() -> None:
    conn, cloud, inf = make_conn(), FakeCloud(), FakeInference()
    r1 = cloud.add("llmhub-a", status=RemoteStatus.TERMINATED)
    r2 = cloud.add("llmhub-b", status=RemoteStatus.EXITED, spend=0.3)
    add_instance(conn, "a", "IDLE", provider_ref=r1.ref, endpoint="e")
    add_instance(conn, "b", "BUSY", pool="q", provider_ref=r2.ref, endpoint="e")
    assert await _alive(conn, cloud, inf, "a", T0) == "lost"
    assert await _alive(conn, cloud, inf, "b", T0) == "lost"
    assert trail(conn, "b")[-1][2] == "stopped_but_billing"


async def test_check_alive_api_error_changes_nothing() -> None:
    conn, cloud, inf = make_conn(), FakeCloud(), FakeInference()
    r = cloud.add("llmhub-a")
    add_instance(conn, "a", "READY", provider_ref=r.ref, endpoint="e")
    cloud.get_fail = True
    assert await _alive(conn, cloud, inf, "a", T0) == "skipped"
    assert inst(conn, "a")["state"] == "READY"


async def test_check_alive_network_loss_after_three_minutes() -> None:
    conn, cloud, inf = make_conn(), FakeCloud(), FakeInference()
    r = cloud.add("llmhub-a")
    add_instance(conn, "a", "READY", provider_ref=r.ref, endpoint="e")
    inf.reachable = False
    assert await _alive(conn, cloud, inf, "a", T0) == "ok"  # starts the timer
    assert inst(conn, "a")["net_fail_since"] == T0
    assert await _alive(conn, cloud, inf, "a", T0 + 180) == "ok"  # exactly 180 s: not yet
    inf.reachable = True  # recovered: timer cleared
    assert await _alive(conn, cloud, inf, "a", T0 + 190) == "ok"
    assert inst(conn, "a")["net_fail_since"] is None
    inf.reachable = False
    assert await _alive(conn, cloud, inf, "a", T0 + 200) == "ok"
    assert await _alive(conn, cloud, inf, "a", T0 + 381) == "network_loss"
    assert inst(conn, "a")["state"] == "ERROR"
    assert trail(conn)[-1] == ("READY", "ERROR", "network_loss")
