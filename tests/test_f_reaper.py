"""F8: reaper run_once / run_loop on the FakeClock (the controller is "dead" unless stepped)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from f_fixtures import cfg_with, count
from harness import TEST_SECRET, Harness, make_job
from llmhub.lifecycle_manager.machine import transition
from llmhub.lifecycle_manager.safety import is_halted, set_key
from llmhub.lifecycle_manager.states import State
from llmhub.persistence.db import tx
from llmhub.provider_adapters.labels import make_label, new_op_id
from llmhub.provider_adapters.mock import Fault


async def _idle(h: Harness) -> None:
    h.submit(make_job(0))
    await h.run_for(150)
    assert h.state_of() == "IDLE"


async def test_reaper_finishes_teardown_when_controller_is_dead(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await _idle(h)
    inst = h.instances()[0]
    with tx(h.conn):
        transition(h.conn, inst["id"], State.IDLE, State.STOPPING, "E14", h.clock.now(),
                   "controller", stop_reason="idle_timeout")  # fmt: skip
    r = h.reaper()
    await h.drive(r.run_once())
    assert h.state_of() == "STOPPING"  # fresh STOPPING belongs to the controller
    h.clock.advance_to(h.clock.now() + 200)
    await h.drive(r.run_once())
    assert h.state_of() == "PENDING_DELETE"
    rep = await h.drive(r.run_once())
    assert rep.stopped == [inst["id"]] and h.state_of() == "STOPPED"
    assert h.provider.live_refs() == []


async def test_reaper_leaked(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.provider.inject("destroy", Fault.DESTROY_NEVER)
    await _idle(h)
    await h.run_for(400)  # idle -> STOPPING -> PENDING_DELETE (accepted, never gone)
    assert h.state_of() == "PENDING_DELETE"
    r = h.reaper()
    h.clock.advance_to(h.clock.now() + 1000)
    rep = await h.drive(r.run_once())
    assert rep.leaked and h.state_of() == "LEAKED" and is_halted(h.conn)
    assert h.incidents("leak")


async def test_reaper_orphan_safe_mode(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    orphan = h.provider.add_orphan(make_label(new_op_id(), TEST_SECRET))
    set_key(h.conn, "safe_mode", "1", h.clock.now())
    rep = await h.drive(h.reaper().run_once())
    assert rep.sweep.destroyed == [orphan] and h.provider.live_refs() == []


async def test_reaper_orphan_stopped_row(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await _idle(h)
    await h.run_for(400)
    assert h.state_of() == "STOPPED"
    label = h.instances()[0]["label"]
    late = h.provider.add_orphan(label)  # a resource still carrying a finished row's label
    rep = await h.drive(h.reaper().run_once())
    assert late in rep.sweep.destroyed and h.provider.live_refs() == []


async def test_live_duplicate_destroyed(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await _idle(h)
    inst = h.instances()[0]
    dup = h.provider.add_orphan(inst["label"])  # late result of an unknown-outcome create
    rep = await h.drive(h.reaper().run_once())
    assert rep.sweep.duplicates == [dup]
    assert h.provider.live_refs() == [inst["provider_ref"]]
    assert h.incidents("duplicate_resource")
    assert h.state_of() == "IDLE"  # the row's own resource is untouched


async def test_reaper_unknown_resource_q1(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    ref = h.provider.add_orphan(make_label(new_op_id(), TEST_SECRET))
    r = h.reaper()
    rep = await h.drive(r.run_once())
    assert rep.sweep.unknown == [ref] and h.provider.live_refs() == [ref]
    inc = h.incidents("unknown_owned_resource")
    assert len(inc) == 1 and inc[0]["severity"] == "critical" and is_halted(h.conn)
    snaps = count(h, "SELECT count(*) FROM provider_snapshots")
    assert snaps == 1  # billing reconcile right away
    h.clock.advance_to(h.clock.now() + 900)
    await h.drive(r.run_once())
    assert h.provider.live_refs() == [ref]
    assert count(h, "SELECT count(*) FROM provider_snapshots") == 2  # every 15 min
    h.clock.advance_to(h.clock.now() + 901)
    rep = await h.drive(r.run_once())
    assert rep.sweep.destroyed == [ref] and h.provider.live_refs() == []
    assert len(h.incidents("unknown_owned_resource")) == 1  # deduplicated


async def test_reaper_never_touches_foreign(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    foreign = h.provider.add_foreign("someone-else")
    forged = h.provider.add_foreign(make_label(new_op_id(), b"another-secret-" * 3))
    set_key(h.conn, "safe_mode", "1", h.clock.now())
    (tmp_path / "KILL").touch()
    r = h.reaper()
    for _ in range(3):
        await h.drive(r.run_once())
        h.clock.advance_to(h.clock.now() + 1900)
    assert h.provider.live_refs() == sorted([foreign, forged])


async def test_hard_cap_with_dead_controller(tmp_path: Path) -> None:
    cfg = cfg_with(tmp_path, budget={"hard_cap_month": 0.05})
    h = Harness(tmp_path, cfg=cfg)
    await _idle(h)  # the controller now stops stepping
    r = h.reaper()
    h.clock.advance_to(h.clock.now() + 600)  # 0.22 $/h * ~750 s ~ 0.046
    rep = await h.drive(r.run_once())
    assert not rep.hard_cap
    h.clock.advance_to(h.clock.now() + 120)
    rep = await h.drive(r.run_once())
    assert rep.hard_cap and rep.kill is not None and rep.kill.completed
    assert h.provider.live_refs() == [] and is_halted(h.conn)
    assert h.incidents("hard_cap")


async def test_accrual_no_double_count(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    t0 = h.clock.now()
    h.submit(make_job(0, max_tokens=78 * 600))
    await h.run_until(t0 + 100)
    r = h.reaper()
    for k in range(6):  # controller and reaper accrue alternately
        await h.run_until(t0 + 100 + 50 * k)
        await h.drive(r.run_once())
    inst = h.instances()[0]
    expected = inst["price_usd_h"] / 3600 * (inst["accrued_to"] - inst["created_at"])
    assert abs(h.ledger_total() - expected) < 1e-9


async def test_lifetime_fuse(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await _idle(h)
    inst = h.instances()[0]
    h.conn.execute("UPDATE instances SET max_lifetime_at=? WHERE id=?",
                   (h.clock.now() - 601, inst["id"]))  # fmt: skip
    rep = await h.drive(h.reaper().run_once())
    assert rep.fused == [inst["id"]]
    assert ("IDLE", "STOPPING", "lifetime_fuse") in h.transitions()
    assert h.state_of() == "PENDING_DELETE" and h.provider.live_refs() == []


async def test_stale_controller_heartbeat(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await _idle(h)
    r = h.reaper()
    assert not (await h.drive(r.run_once())).stale_controller
    h.clock.advance_to(h.clock.now() + 700)
    assert (await h.drive(r.run_once())).stale_controller
    assert h.incidents("controller_stale")


async def test_reaper_only_kill_file_zero_within_60s(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await _idle(h)
    h.provider.add_orphan(make_label(new_op_id(), TEST_SECRET))
    stop = asyncio.Event()
    loop_task = asyncio.ensure_future(h.reaper().run_loop(15, stop))
    await h.drive(h.clock.sleep_until(h.clock.now() + 7))  # KILL lands mid-interval
    (tmp_path / "KILL").touch()
    created = h.clock.now()
    await h.drive(h.clock.sleep_until(created + 60))
    assert h.provider.live_refs() == []
    stop.set()
    await h.drive(h.clock.sleep_until(h.clock.now() + 16))
    await asyncio.wait_for(loop_task, 1)
