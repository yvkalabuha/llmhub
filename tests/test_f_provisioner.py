"""F2: PROVISIONING -> READY, unknown create outcomes (section 7.3), balance, offers, ghost."""

from __future__ import annotations

import json
from pathlib import Path

from f_fixtures import cfg_with, count, edges, job_states
from harness import Harness, make_job
from llmhub.cloud_manager.provisioner import find_duplicates
from llmhub.lifecycle_manager.safety import is_blocked
from llmhub.provider_adapters.base import RemoteResource, RemoteStatus, ResourceKind
from llmhub.provider_adapters.mock import Fault, MockKnobs


async def test_path_to_ready_records_intent_and_offer(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 30)
    inst = h.instances()[0]
    assert inst["create_called_at"] == t0 and inst["attempts"] == 1
    offer = json.loads(inst["offer_json"])
    assert offer["host_id"] == "host-0" and "disk_usd_h" in offer
    await h.run_until(t0 + 70)
    assert edges(h)[:4] == [("STOPPED", "PROVISIONING"), ("PROVISIONING", "BOOTING"),
                            ("BOOTING", "LOADING_MODEL"), ("LOADING_MODEL", "READY")]  # fmt: skip


async def test_create_unknown_adopt(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.provider.inject("create", Fault.CREATE_TIMEOUT_CREATED)
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 100)
    inst = h.instances()[0]
    assert h.provider.create_calls == 1 and inst["attempts"] == 1
    assert h.provider.live_refs() == [inst["provider_ref"]]
    await h.run_until(t0 + 1200)
    assert job_states(h) == [("completed", "cloud")] and h.provider.live_refs() == []


async def test_create_unknown_retry_same_label(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.provider.inject("create", Fault.CREATE_TIMEOUT_NOT_CREATED)
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 110)
    assert h.provider.create_calls == 1 and h.provider.live_refs() == []
    await h.run_until(t0 + 200)  # grace 120 s passed: retry with the same label
    inst = h.instances()[0]
    assert h.provider.create_calls == 2 and inst["attempts"] == 2
    assert len(h.provider.live_refs()) == 1
    rows = h.provider.store.conn.execute("SELECT label FROM resources").fetchall()
    assert {r["label"] for r in rows} == {inst["label"]}
    await h.run_until(t0 + 1500)
    assert job_states(h) == [("completed", "cloud")]


async def test_create_unknown_late_duplicate(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.provider.inject("create", Fault.CREATE_TIMEOUT_CREATED_LATE)
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 200)
    assert len(h.provider.live_refs()) == 2  # the late one is billing but still invisible
    await h.run_until(t0 + 420)  # visible at +300, the duplicate sweep runs every 60 s
    inst = h.instances()[0]
    assert h.provider.live_refs() == [inst["provider_ref"]]
    assert h.incidents("duplicate_resource")
    await h.run_until(t0 + 2000)
    assert h.provider.live_refs() == [] and job_states(h) == [("completed", "cloud")]


async def test_create_unknown_max_attempts(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.provider.inject("create", Fault.CREATE_TIMEOUT_NOT_CREATED, times=3)
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 600)
    assert h.provider.create_calls == 2
    assert ("PROVISIONING", "ERROR", "create_unknown_max_attempts") in h.transitions()
    job = h.jobs()[0]
    assert (job["route"], job["fallback_reason"]) == ("local:fallback", "deployment_error")
    assert h.provider.live_refs() == [] and h.state_of() == "STOPPED"


async def test_insufficient_balance_no_retry(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.provider.inject("create", Fault.INSUFFICIENT_BALANCE)
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 60)
    assert h.provider.create_calls == 1
    assert is_blocked(h.conn, "mock") == "insufficient_balance"
    assert ("PROVISIONING", "ERROR", "insufficient_balance") in h.transitions()
    assert h.jobs()[0]["fallback_reason"] == "insufficient_balance"
    h.provider.knobs.balance_usd = 1000.0  # money arrived: the block stays
    h.submit(make_job(1))
    await h.run_until(t0 + 600)
    assert h.provider.create_calls == 1
    assert [j["fallback_reason"] for j in h.jobs()] == ["insufficient_balance"] * 2
    h.controller.resume("mock")
    h.submit(make_job(2))
    await h.run_until(t0 + 2000)
    assert h.provider.create_calls == 2 and h.jobs()[-1]["status"] == "completed"


async def test_gpu_unavailable_next_offer(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.provider.inject("create", Fault.GPU_UNAVAILABLE)
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 120)
    inst = h.instances()[0]
    assert h.provider.create_calls == 2 and inst["host_id"] == "host-1"
    assert inst["attempts"] == 1  # the excluded offer is not a repeated attempt


async def test_gpu_unavailable_offer_wait(tmp_path: Path) -> None:
    h = Harness(tmp_path, knobs=MockKnobs(gpu_available=False))
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 590)
    assert h.state_of() == "PROVISIONING" and h.provider.create_calls == 0
    await h.run_until(t0 + 700)
    job = h.jobs()[0]
    assert (job["route"], job["fallback_reason"]) == ("local:fallback", "gpu_unavailable")
    assert h.state_of() == "STOPPED"


async def test_create_ghost_ref(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.provider.inject("create", Fault.CREATE_GHOST_REF)
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 150)
    inst = h.instances()[0]
    real = h.provider.live_refs()
    assert len(real) == 1 and inst["provider_ref"] == real[0]
    assert h.incidents("ghost_ref")
    await h.run_until(t0 + 1200)
    assert job_states(h) == [("completed", "cloud")] and h.provider.live_refs() == []


async def test_provisioning_with_ref_after_restart_polls_until_running(tmp_path: Path) -> None:
    h = Harness(tmp_path, knobs=MockKnobs(boot_s=100))
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 1)
    assert h.state_of() == "BOOTING"
    # simulate a crash right after the ref was stored but before E2 was committed
    h.conn.execute("UPDATE instances SET state='PROVISIONING'")
    h.restart()
    await h.run_until(t0 + 50)
    assert h.state_of() == "PROVISIONING"  # get = PENDING: keep polling
    await h.run_until(t0 + 110)
    assert h.state_of() in ("BOOTING", "LOADING_MODEL", "READY", "BUSY")
    await h.run_until(t0 + 1500)
    assert job_states(h) == [("completed", "cloud")] and h.provider.create_calls == 1


def test_find_duplicates_rule() -> None:
    def rr(ref: str, label: str, at: float) -> RemoteResource:
        return RemoteResource("mock", ref, label, ResourceKind.INSTANCE, RemoteStatus.RUNNING,
                              None, None, 0.2, 0.2, at)  # fmt: skip

    owned = [rr("a", "L", 5), rr("b", "L", 1), rr("c", "other", 0)]
    with_ref = {"label": "L", "provider_ref": "a"}
    assert [r.ref for r in find_duplicates(owned, with_ref)] == ["b"]  # type: ignore[arg-type]
    no_ref = {"label": "L", "provider_ref": None}
    assert [r.ref for r in find_duplicates(owned, no_ref)] == ["a"]  # type: ignore[arg-type]


async def test_breaker_open_create_not_attempted(tmp_path: Path) -> None:
    cfg = cfg_with(tmp_path, retry={"breaker_errors": 1})
    h = Harness(tmp_path, cfg=cfg)
    h.provider.inject("create", Fault.CREATE_5XX)
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 30)
    assert h.provider.create_calls == 1
    assert count(h, "SELECT count(*) FROM provider_accounts WHERE circuit_state='open'") == 1
    await h.run_until(t0 + 400)  # open for 900 s: no create calls, attempt not burned
    assert h.provider.create_calls == 1 and h.instances()[0]["attempts"] == 1
