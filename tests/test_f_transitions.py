"""F4: every edge of the state machine is produced by a Harness scenario (guard test), E11
(BUSY -> BUSY, no transitions row) separately."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from f_fixtures import cfg_with
from harness import CLIENT_ID, Harness, make_job
from llmhub.lifecycle_manager import leases
from llmhub.lifecycle_manager.states import TRANSITIONS, State
from llmhub.provider_adapters.mock import Fault, MockKnobs
from llmhub.provider_adapters.mock_inference import MockInferenceKnobs
from llmhub.task_queue import queue

Key = tuple[str, str, str]  # (src, dst, edge id)
Scenario = Callable[[Path], Awaitable[Harness]]
LONG = 78 * 600  # max_tokens for a ~600 s job (share stays under budget_job)


async def _basic(tmp: Path, until: float = 2000, **kw: object) -> Harness:
    h = Harness(tmp, **kw)  # type: ignore[arg-type]
    h.submit(make_job(0))
    await h.run_for(until)
    return h


async def _prewarm(tmp: Path, **kw: object) -> Harness:
    h = Harness(tmp, **kw)  # type: ignore[arg-type]
    leases.create_explicit(h.conn, holder="agent", client_id=CLIENT_ID, pool="p1", ttl_s=3600,
                           now=h.clock.now(), prewarm=True)  # fmt: skip
    await h.run_for(120)
    assert h.state_of() == "READY"
    return h


async def _kill(h: Harness) -> Harness:
    await h.drive(h.controller.kill())
    return h


async def s_e3(tmp: Path) -> Harness:
    h = Harness(tmp)
    h.provider.inject("create", Fault.INSUFFICIENT_BALANCE)
    h.submit(make_job(0))
    await h.run_for(60)
    return h


async def s_e5(tmp: Path) -> Harness:
    return await _basic(tmp, 900, knobs=MockKnobs(boot_s=700))


async def s_e7(tmp: Path) -> Harness:
    return await _basic(tmp, 200, inference_knobs=MockInferenceKnobs(test_fail=3))


async def s_e9(tmp: Path) -> Harness:
    h = Harness(tmp)
    res = h.submit(make_job(0))
    await h.run_for(5)
    queue.cancel(h.conn, res.job_id, h.clock.now())
    await h.run_for(200)
    return h


async def s_e10(tmp: Path) -> Harness:
    h = await _prewarm(tmp)
    h.provider.interrupt(h.instances()[0]["provider_ref"])
    await h.run_for(120)
    return h


async def s_e13(tmp: Path) -> Harness:
    h = await _basic(tmp, 100)
    assert h.state_of() == "IDLE"
    h.submit(make_job(1))
    await h.run_for(50)
    return h


async def s_e15_ready(tmp: Path) -> Harness:
    return await _kill(await _prewarm(tmp))


async def s_e15_idle(tmp: Path) -> Harness:
    h = await _basic(tmp, 100)
    assert h.state_of() == "IDLE"
    return await _kill(h)


async def s_e15_busy(tmp: Path) -> Harness:
    h = Harness(tmp)
    h.submit(make_job(0, max_tokens=LONG))
    await h.run_for(200)
    assert h.state_of() == "BUSY"
    return await _kill(h)


async def s_e15_provisioning(tmp: Path) -> Harness:
    h = Harness(tmp, knobs=MockKnobs(gpu_available=False))
    h.submit(make_job(0))
    await h.run_for(30)
    return await _kill(h)


async def s_e15_booting(tmp: Path) -> Harness:
    h = Harness(tmp, knobs=MockKnobs(boot_s=300))
    h.submit(make_job(0))
    await h.run_for(30)
    assert h.state_of() == "BOOTING"
    return await _kill(h)


async def s_e15_loading(tmp: Path) -> Harness:
    h = Harness(tmp, inference_knobs=MockInferenceKnobs(load_s=300))
    h.submit(make_job(0))
    await h.run_for(100)
    assert h.state_of() == "LOADING_MODEL"
    return await _kill(h)


async def s_e16(tmp: Path) -> Harness:
    cfg = cfg_with(tmp, timeouts={"max_lifetime_s": 600})
    h = Harness(tmp, cfg=cfg)
    h.submit(make_job(0, max_tokens=LONG))
    await h.run_for(1500)
    return h


async def s_e17_busy(tmp: Path) -> Harness:
    h = Harness(tmp)
    h.submit(make_job(0, max_tokens=LONG))
    await h.run_for(200)
    h.provider.interrupt(h.instances()[0]["provider_ref"])
    await h.run_for(100)
    return h


async def s_e17_idle(tmp: Path) -> Harness:
    h = await _basic(tmp, 100)
    assert h.state_of() == "IDLE"
    h.provider.interrupt(h.instances()[0]["provider_ref"])
    await h.run_for(100)
    return h


async def s_e21(tmp: Path) -> Harness:
    h = Harness(tmp)
    h.provider.inject("destroy", Fault.DESTROY_NEVER)
    h.submit(make_job(0))
    await h.run_for(400 + 1000)
    return h


async def s_e22(tmp: Path) -> Harness:
    h = await s_e21(tmp)
    assert h.state_of() == "LEAKED"
    h.provider.store.conn.execute("UPDATE resources SET immortal=0")
    await h.run_for(200)
    return h


S = State
SCENARIOS: dict[Key, tuple[Scenario, str | None]] = {
    (S.STOPPED, S.PROVISIONING, "E1"): (lambda t: _basic(t, 1), None),
    (S.PROVISIONING, S.BOOTING, "E2"): (lambda t: _basic(t, 5), None),
    (S.PROVISIONING, S.ERROR, "E3"): (s_e3, None),
    (S.BOOTING, S.LOADING_MODEL, "E4"): (lambda t: _basic(t, 100), None),
    (S.BOOTING, S.ERROR, "E5"): (s_e5, "booting_timeout"),
    (S.LOADING_MODEL, S.READY, "E6"): (lambda t: _basic(t, 100), None),
    (S.LOADING_MODEL, S.ERROR, "E7"): (s_e7, "model_test_failed"),
    (S.READY, S.BUSY, "E8"): (lambda t: _basic(t, 100), None),
    (S.READY, S.IDLE, "E9"): (s_e9, None),
    (S.READY, S.ERROR, "E10"): (s_e10, None),
    (S.BUSY, S.IDLE, "E12"): (lambda t: _basic(t, 100), None),
    (S.IDLE, S.BUSY, "E13"): (s_e13, None),
    (S.IDLE, S.STOPPING, "E14"): (lambda t: _basic(t, 1000), "idle_timeout"),
    (S.READY, S.STOPPING, "E15"): (s_e15_ready, "admin"),
    (S.IDLE, S.STOPPING, "E15"): (s_e15_idle, "admin"),
    (S.BUSY, S.STOPPING, "E15"): (s_e15_busy, "admin"),
    (S.PROVISIONING, S.STOPPING, "E15"): (s_e15_provisioning, "admin"),
    (S.BOOTING, S.STOPPING, "E15"): (s_e15_booting, "admin"),
    (S.LOADING_MODEL, S.STOPPING, "E15"): (s_e15_loading, "admin"),
    (S.BUSY, S.STOPPING, "E16"): (s_e16, "E16"),
    (S.BUSY, S.ERROR, "E17"): (s_e17_busy, None),
    (S.IDLE, S.ERROR, "E17"): (s_e17_idle, None),
    (S.ERROR, S.STOPPING, "E18"): (s_e3, None),
    (S.STOPPING, S.PENDING_DELETE, "E19"): (lambda t: _basic(t, 1000), "E19"),
    (S.PENDING_DELETE, S.STOPPED, "E20"): (lambda t: _basic(t, 1000), "E20"),
    (S.PENDING_DELETE, S.LEAKED, "E21"): (s_e21, "leak"),
    (S.LEAKED, S.STOPPED, "E22"): (s_e22, "E22"),
}


def test_guard_every_edge_has_a_scenario() -> None:
    edges = {(e.src, e.dst, e.trigger) for e in TRANSITIONS}
    assert set(SCENARIOS) | {(S.BUSY, S.BUSY, "E11")} == edges
    assert len(SCENARIOS) == len(TRANSITIONS) - 1


@pytest.mark.parametrize("key", sorted(SCENARIOS), ids=lambda k: f"{k[2]}-{k[0]}-{k[1]}")
async def test_edge(tmp_path: Path, key: Key) -> None:
    scenario, reason = SCENARIOS[key]
    h = await scenario(tmp_path)
    rows = h.transitions()
    hits = [r for r in rows if (r[0], r[1]) == (key[0], key[1])]
    assert hits, f"{key} not produced; trail={rows}"
    if reason is not None:
        assert any(r[2] == reason for r in hits), hits


async def test_e11_busy_to_busy_without_row(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.submit(make_job(0))
    h.submit(make_job(1))
    await h.run_for(2000)
    assert [j["status"] for j in h.jobs()] == ["completed", "completed"]
    rows = [(a, b) for a, b, _ in h.transitions()]
    assert rows.count(("READY", "BUSY")) == 1 and rows.count(("BUSY", "IDLE")) == 1
    assert ("BUSY", "BUSY") not in rows and ("IDLE", "BUSY") not in rows
    assert h.provider.create_calls == 1
    j0, j1 = sorted(h.jobs(), key=lambda j: j["started_at"])
    assert j1["started_at"] == j0["finished_at"]  # handed out at once, no IDLE in between
