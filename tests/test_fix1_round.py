"""T-368 review fix round 1: tests for fixes not pinned by the review reproductions.

The reproductions themselves live in `test_review1_logic.py` and `test_review2_failures.py`;
this file covers the remaining parts of each fix (see the task journal «Раунд исправлений 1»).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import test_c_support as C
from d_fixtures import Env, spec
from f_fixtures import cfg_with
from harness import TEST_SECRET, Harness, make_job
from llmhub.budget_manager import limits
from llmhub.budget_manager import reservations as rs
from llmhub.budget_manager.limits import Gate, Stage
from llmhub.killswitch import KILL_FILE
from llmhub.lifecycle_manager import leases, machine, teardown
from llmhub.lifecycle_manager.safety import get_key, is_halted, set_key
from llmhub.persistence.db import Busy, connect, migrate, tx
from llmhub.provider_adapters.labels import make_label, new_op_id
from llmhub.provider_adapters.mock import Fault
from llmhub.reaper import core as reaper_core
from llmhub.reaper.__main__ import main as reaper_main
from llmhub.task_queue import queue
from llmhub.task_queue.models import Priority


async def _idle(h: Harness) -> None:
    h.submit(make_job(0))
    await h.run_for(150)
    assert h.state_of() == "IDLE"


# --- F-R2-1 -------------------------------------------------------------------------------------
async def test_controller_sweeps_resource_of_stopped_row(tmp_path: Path) -> None:
    """The controller alone destroys an HMAC resource whose row is STOPPED (every 60 s)."""
    h = Harness(tmp_path)
    await _idle(h)
    await h.run_for(400)
    assert h.state_of() == "STOPPED"
    late = h.provider.add_orphan(h.instances()[0]["label"])
    await h.run_for(120)
    assert late not in h.provider.live_refs()


async def test_unknown_outcome_waits_first_create_plus_n_grace() -> None:
    conn, cloud, cfg = C.make_conn(), C.FakeCloud(), C.make_cfg()
    grace = cfg.timeouts.create_visibility_grace_s
    C.add_instance(conn, "a", "PENDING_DELETE", attempts=2, create_unknown=1,
                   first_create_called_at=C.T0, last_create_called_at=C.T0 + 100,
                   destroy_requested_at=C.T0 + 200)  # fmt: skip
    wait = C.T0 + teardown.UNKNOWN_GRACE_FACTOR * grace
    assert teardown.no_ref_wait_until(C.inst(conn, "a"), cfg) == wait
    assert not await teardown.confirm_zero(conn, cloud, C.inst(conn, "a"), wait - 1, cfg, "reaper")
    assert await teardown.confirm_zero(conn, cloud, C.inst(conn, "a"), wait, cfg, "reaper")


async def test_resource_visible_during_the_wait_is_destroyed_at_once() -> None:
    conn, cloud, cfg = C.make_conn(), C.FakeCloud(), C.make_cfg()
    C.add_instance(conn, "a", "PENDING_DELETE", attempts=1, create_unknown=1,
                   first_create_called_at=C.T0, last_create_called_at=C.T0)  # fmt: skip
    res = cloud.add("llmhub-a")
    assert not await teardown.confirm_zero(conn, cloud, C.inst(conn, "a"), C.T0 + 10, cfg, "reaper")
    assert res.ref not in cloud.resources


# --- F-R2-3 -------------------------------------------------------------------------------------
async def test_row_of_disabled_provider_is_an_incident_not_a_crash(tmp_path: Path) -> None:
    h = Harness(tmp_path, clients=("c1",))
    C.add_instance(h.conn, "x", "IDLE", pool="px", provider="gone", provider_ref="r1",
                   endpoint="e", idle_since=h.clock.now())  # fmt: skip
    C.add_job(h.conn, "jx", "queued", pool="px")
    await h.step()
    await h.step()
    row = h.conn.execute("SELECT * FROM instances WHERE id='x'").fetchone()
    assert (row["state"], row["stop_reason"]) == ("STOPPING", "pool_removed")
    assert h.incidents("adapter_missing")
    job = h.conn.execute("SELECT * FROM jobs WHERE id='jx'").fetchone()
    assert (job["route"], job["fallback_reason"]) == ("local:fallback", "pool_removed")


# --- F-R2-4 -------------------------------------------------------------------------------------
async def test_reaper_loop_reconnects_after_quarantine(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    r = h.reaper(db_path=h.db_path)
    (tmp_path / "state.db").rename(tmp_path / "state.db.corrupt-1")
    for side in ("state.db-wal", "state.db-shm"):
        p = tmp_path / side
        if p.exists():
            p.rename(tmp_path / f"{side}.corrupt-1")
    fresh = connect(tmp_path / "state.db")
    migrate(fresh)
    set_key(fresh, "safe_mode", "1", h.clock.now())
    stop = asyncio.Event()
    task = asyncio.ensure_future(r.run_loop(15, stop))
    await h._settle()
    stop.set()
    h.clock.advance_to(h.clock.now() + 15)
    await asyncio.wait_for(task, 1)
    assert get_key(r.conn, "safe_mode") == "1"


async def test_reaper_loop_corrupt_db_honours_kill(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    ours = h.provider.add_orphan(make_label(new_op_id(), TEST_SECRET))
    foreign = h.provider.add_orphan("llmhub-someone-else")
    r = h.reaper(db_path=h.db_path)
    r.conn.close()
    h.conn.close()
    (tmp_path / "state.db").write_bytes(b"garbage" * 1000)
    for side in ("state.db-wal", "state.db-shm"):
        (tmp_path / side).unlink(missing_ok=True)
    (tmp_path / KILL_FILE).write_text("stop", encoding="utf-8")
    r._need_reopen = True
    stop = asyncio.Event()
    task = asyncio.ensure_future(r.run_loop(15, stop))
    for _ in range(50):
        await asyncio.sleep(0)
    stop.set()
    h.clock.advance_to(h.clock.now() + 15)
    await asyncio.wait_for(task, 1)
    assert ours not in h.provider.live_refs() and foreign in h.provider.live_refs()


# --- L1 / L2 / L3 / L6 / L8 ------------------------------------------------------------------------
async def test_reaper_steps_are_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(tmp_path)
    orphan = h.provider.add_orphan(make_label(new_op_id(), TEST_SECRET))
    set_key(h.conn, "safe_mode", "1", h.clock.now())

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("accrual failed")

    monkeypatch.setattr(reaper_core.ledger, "accrue", boom)
    rep = await h.drive(h.reaper().run_once())
    assert rep.failed_steps == ["hard_cap"]
    assert rep.sweep.destroyed == [orphan]  # the later steps still ran
    assert h.incidents("reaper_step_failed")


async def test_persistent_kill_runs_kill_all_once_then_short_passes(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.provider.inject("destroy", Fault.DESTROY_NEVER)
    h.provider.add_orphan(make_label(new_op_id(), TEST_SECRET))
    (tmp_path / KILL_FILE).write_text("stop", encoding="utf-8")
    r = h.reaper()
    t0 = h.clock.now()
    rep1 = await h.drive(r.run_once())
    assert rep1.kill is not None and not rep1.kill.completed
    assert h.clock.now() - t0 >= 45  # the first run waits for the deadline
    for _ in range(3):
        h.clock.advance_to(h.clock.now() + 15)
        t1 = h.clock.now()
        rep = await h.drive(r.run_once())
        assert rep.kill is not None and h.clock.now() - t1 < 5  # one non-blocking pass
    assert len(h.incidents("kill_incomplete")) == 1


def test_reaper_loop_below_5_s_is_rejected(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"data_dir: {tmp_path}\npools: {{}}\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        reaper_main(["--config", str(cfg), "--loop", "1"])
    assert exc.value.code == 2


async def test_unknown_key_dropped_when_resource_disappears(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    ref = h.provider.add_orphan(make_label(new_op_id(), TEST_SECRET))
    r = h.reaper()
    rep = await h.drive(r.run_once())
    assert rep.sweep.unknown == [ref] and get_key(h.conn, f"unknown_seen:{ref}") is not None
    await h.drive(h.provider.destroy(ref))  # gone by itself (e.g. an admin removed it)
    await h.drive(r.run_once())
    assert get_key(h.conn, f"unknown_seen:{ref}") is None


async def test_destroyed_unknown_seen_again_is_no_new_halt(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    ref = h.provider.add_orphan(make_label(new_op_id(), TEST_SECRET))
    r = h.reaper()
    await h.drive(r.run_once())
    h.clock.advance_to(h.clock.now() + reaper_core.UNKNOWN_DESTROY_AFTER_S)
    rep = await h.drive(r.run_once())
    assert ref in rep.sweep.destroyed
    with tx(h.conn):
        h.conn.execute("DELETE FROM controller_state WHERE key IN ('halted','halt_reason')")
    # a stale list shows the destroyed resource again: destroy again, no new HALT
    await reaper_core._unknown_resource(
        h.conn, h.provider, _res(h, ref), h.clock.now(), h.cfg, reaper_core.SweepReport()
    )
    assert not is_halted(h.conn)


def _res(h: Harness, ref: str) -> Any:
    from llmhub.provider_adapters.base import RemoteResource, RemoteStatus, ResourceKind

    row = h.provider.store.conn.execute("SELECT * FROM resources WHERE ref=?", (ref,)).fetchone()
    return RemoteResource(
        provider="mock", ref=ref, label=row["label"], kind=ResourceKind.INSTANCE,
        status=RemoteStatus.RUNNING, endpoint=None, host_id=None, price_usd_h=0.0,
        spend_rate_usd_h=0.0, created_at=0.0,
    )  # fmt: skip


async def test_reaper_stops_idle_gpu_of_dead_controller(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.cfg = cfg_with(tmp_path, pool={"idle_timeout_s": 900})
    h.restart()
    await _idle(h)
    r = h.reaper()
    h.clock.advance_to(h.clock.now() + reaper_core.HEARTBEAT_STALE_S + 1)
    rep = await h.drive(r.run_once())
    assert rep.stale_controller and rep.dead_stopped == []  # 10 min: report only
    h.clock.advance_to(h.clock.now() + 300)
    rep = await h.drive(r.run_once())
    assert rep.dead_stopped == [h.instances()[0]["id"]]
    assert h.state_of() in ("STOPPING", "PENDING_DELETE")
    assert h.incidents("controller_dead_stop")


# --- L5 -----------------------------------------------------------------------------------------
def test_job_restarted_too_often_goes_local(tmp_path: Path) -> None:
    env = Env(tmp_path)
    jid = queue.submit(env.conn, env.bodies, spec(1), 0.0, env.cfg).job_id
    queue.mark_running(env.conn, jid, "i1", "b0", 0.0)
    for i in range(queue.MAX_RESTARTS + 1):
        queue.requeue_orphaned_running(env.conn, f"b{i + 1}", 0.0, 180)
        queue.mark_running(env.conn, jid, "i1", f"b{i + 1}", 0.0)
    j = env.job(jid)
    assert (j["route"], j["fallback_reason"]) == ("local:fallback", "max_restarts")
    n = env.conn.execute("SELECT count(*) FROM incidents WHERE kind='job_max_restarts'")
    assert n.fetchone()[0] == 1


# --- R1-2 / R1-8 --------------------------------------------------------------------------------
def test_hold_reservation_bound_at_e1_and_unbound_at_e20() -> None:
    conn = C.make_conn()
    lid = leases.create_hold(conn, holder="admin", pool=C.POOL, ttl_s=7200, now=C.T0)
    rid = rs.reserve(conn, "hold", lid, 0.5, C.T0, None)
    iid = machine.insert_provisioning(
        conn, pool=C.POOL, provider="fake", op_id="op1", label="llmhub-op1",
        max_lifetime_at=C.T0 + 10800, now=C.T0, gate=lambda _c: Gate(ok=True),
    )  # fmt: skip
    row = conn.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
    assert row["instance_id"] == iid
    with tx(conn):
        rs.release_instance(conn, str(iid), C.T0 + 100, "instance_stopped")
    row = conn.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
    assert row["instance_id"] is None and row["released_at"] is None  # the lease still lives


def test_hold_renew_extends_live_instance_lifetime() -> None:
    conn = C.make_conn()
    lid = leases.create_hold(conn, holder="admin", pool=C.POOL, ttl_s=4 * 3600, now=C.T0)
    C.add_instance(conn, "a", "IDLE")  # launched later: max_lifetime_at = T0 + 3 h
    leases.renew(conn, lid, C.T0 + 2 * 3600)
    assert float(C.inst(conn, "a")["max_lifetime_at"]) == C.T0 + 6 * 3600


# --- R1-6 / info (URGENT_ONLY) ----------------------------------------------------------------------
async def test_no_launch_stops_ready_instance(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await _idle(h)
    iid = h.instances()[0]["id"]
    h.conn.execute("UPDATE instances SET state='READY', idle_since=NULL WHERE id=?", (iid,))
    h.controller._budget(h.clock.now(), True)
    assert h.instances()[0]["stop_after_current"] == 1


async def test_urgent_only_launch_takes_only_urgent_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    real = limits.stage
    monkeypatch.setattr(limits, "stage", lambda *_a, **_k: Stage.URGENT_ONLY)
    normal = h.submit(make_job(1)).job_id
    urgent = h.submit(make_job(2, priority=Priority.URGENT)).job_id
    await h.step()
    iid = h.instances()[0]["id"]
    assert json.loads(get_key(h.conn, f"launch_jobs:{iid}") or "[]") == [urgent]
    await h.run_for(600)
    jobs = {j["id"]: j for j in h.jobs()}
    assert (jobs[urgent]["status"], jobs[urgent]["route"]) == ("completed", "cloud")
    assert jobs[normal]["route"] == "local:fallback"
    monkeypatch.setattr(limits, "stage", real)


# --- R1-9 ---------------------------------------------------------------------------------------
async def test_failed_cycle_counted_once_with_e18(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    C.add_instance(h.conn, "e", "ERROR", pool="p1", provider="mock")
    h.conn.execute(
        "INSERT INTO transitions(instance_id, ts, from_state, to_state, reason, actor)"
        " VALUES ('e', 0, 'PROVISIONING', 'ERROR', 'E3', 'controller')"
    )
    h.controller._before_error_to_stopping(h.clock.now())
    h.controller._before_error_to_stopping(h.clock.now())
    assert get_key(h.conn, "failed_cycles:p1") == "1"
    assert C.inst(h.conn, "e")["state"] == "STOPPING"


# --- R1-10 --------------------------------------------------------------------------------------
def test_requeue_releases_the_job_reservation(tmp_path: Path) -> None:
    env = Env(tmp_path)
    jid = queue.submit(env.conn, env.bodies, spec(1), 0.0, env.cfg).job_id
    queue.mark_running(env.conn, jid, "i1", "b0", 0.0)
    rs.reserve(env.conn, "job", jid, 0.1, 0.0, "i1")
    queue.requeue(env.conn, jid, 1.0, count_attempt=False, reason="network_lost")
    open_rows = env.conn.execute(
        "SELECT count(*) FROM reservations WHERE ref_id=? AND released_at IS NULL", (jid,)
    ).fetchone()[0]
    assert open_rows == 0


async def test_completion_lost_to_busy_is_written_by_the_next_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    real = queue.complete
    calls = {"n": 0}

    def flaky(*a: Any, **k: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise Busy("locked")
        real(*a, **k)

    monkeypatch.setattr(queue, "complete", flaky)
    jid = h.submit(make_job(0)).job_id
    await h.run_for(150)
    job = h.conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    assert calls["n"] == 2 and job["status"] == "completed" and job["restarts"] == 0


# --- R1-11 --------------------------------------------------------------------------------------
async def test_foreign_resource_under_our_ref_is_not_destroyed() -> None:
    conn, cloud, cfg = C.make_conn(), C.FakeCloud(), C.make_cfg()
    res = cloud.add("llmhub-someone-else")  # the provider reused the id
    C.add_instance(conn, "a", "STOPPING", provider_ref=res.ref)
    await teardown.request_destroy(conn, cloud, C.inst(conn, "a"), C.T0, cfg, "controller")
    assert res.ref in cloud.resources and cloud.destroy_calls == []
    n = conn.execute("SELECT count(*) FROM incidents WHERE kind='ownership_mismatch'")
    assert n.fetchone()[0] == 1


# --- R1-12 root cause ---------------------------------------------------------------------------
async def test_late_adopt_of_the_rows_own_live_ref_destroys_nothing() -> None:
    """A second actor adopts the resource another actor already adopted and moved to BOOTING:
    the row's own live resource must not be destroyed (was the flake of the 10-thread test)."""
    conn, cloud = C.make_conn(), C.FakeCloud()
    res = cloud.add("llmhub-a")
    C.add_instance(conn, "a", "BOOTING", provider_ref=res.ref)
    rr = (await cloud.list_owned("llmhub-a"))[0]
    assert await teardown.adopt_ref_or_destroy(conn, cloud, "a", rr, C.T0) is False
    assert cloud.destroy_calls == [] and C.inst(conn, "a")["provider_ref"] == res.ref


async def test_late_duplicate_of_a_live_row_is_destroyed_without_overwrite() -> None:
    conn, cloud = C.make_conn(), C.FakeCloud()
    mine, dup = cloud.add("llmhub-a"), cloud.add("llmhub-a")
    C.add_instance(conn, "a", "BOOTING", provider_ref=mine.ref)
    rr = next(r for r in await cloud.list_owned("llmhub-a") if r.ref == dup.ref)
    assert await teardown.adopt_ref_or_destroy(conn, cloud, "a", rr, C.T0) is False
    assert cloud.destroy_calls == [dup.ref] and C.inst(conn, "a")["provider_ref"] == mine.ref
