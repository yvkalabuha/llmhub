"""F5: the 19 lifecycle situations of research section 6.3 (plan section 10.2), one test each."""

from __future__ import annotations

from pathlib import Path

import pytest

from f_fixtures import count, edges, job_states
from harness import CLIENT_ID, TEST_SECRET, Harness, make_job
from llmhub.budget_manager import reservations
from llmhub.controller import Controller
from llmhub.lifecycle_manager import leases
from llmhub.lifecycle_manager.leases import LeaseError
from llmhub.lifecycle_manager.safety import is_blocked, is_halted
from llmhub.persistence.integrity import open_state_db
from llmhub.provider_adapters.base import InstanceSpec, RemoteResource
from llmhub.provider_adapters.labels import make_label, new_op_id
from llmhub.provider_adapters.mock import Fault, MockKnobs
from llmhub.provider_adapters.mock_inference import MockInferenceKnobs
from llmhub.task_queue.models import Priority

LONG = 78 * 600


async def test_s01_first_job_stopped(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    seen: list[tuple[int, object, object]] = []
    real_create = h.provider.create

    async def spy(spec: InstanceSpec, label: str) -> RemoteResource:
        row = h.conn.execute("SELECT * FROM instances WHERE label=?", (label,)).fetchone()
        res = count(h, "SELECT count(*) FROM reservations WHERE scope='session'")
        seen.append((res, row and row["state"], row and row["create_called_at"]))
        return await real_create(spec, label)

    h.provider.create = spy  # type: ignore[method-assign]
    res = h.submit(make_job(0))
    lease = h.conn.execute("SELECT kind FROM leases WHERE job_id=?", (res.job_id,)).fetchone()
    assert lease["kind"] == "implicit"
    await h.run_for(30)
    assert seen and seen[0][0] == 1 and seen[0][1] == "PROVISIONING" and seen[0][2] is not None


async def test_s02_requests_during_provisioning(tmp_path: Path) -> None:
    h = Harness(tmp_path, clients=("a", "b"))
    h.submit(make_job(0, client="a"))
    await h.run_for(10)
    assert h.state_of() in ("PROVISIONING", "BOOTING")
    prios = [Priority.LOW, Priority.NORMAL, Priority.URGENT, Priority.NORMAL]
    for i, p in enumerate(prios):
        h.submit(make_job(i + 1, priority=p, client="b"))
    await h.run_for(2000)
    assert h.provider.create_calls == 1
    order = [j["priority"] for j in sorted(h.jobs(), key=lambda j: j["started_at"])]
    assert order == ["urgent", "normal", "normal", "normal", "low"]  # all queued before READY


async def test_s03_job_during_busy(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.submit(make_job(0, max_tokens=78 * 60))
    await h.run_for(100)
    assert h.state_of() == "BUSY"
    h.submit(make_job(1))
    await h.run_for(1000)
    assert edges(h).count(("BUSY", "IDLE")) == 1 and h.provider.create_calls == 1


async def test_s04_job_in_idle(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.submit(make_job(0))
    await h.run_for(150)
    h.submit(make_job(1))
    await h.run_for(30)
    assert ("IDLE", "BUSY") in edges(h) and h.provider.create_calls == 1


async def test_s05_job_during_stopping(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.provider.inject("destroy", Fault.DESTROY_FAIL, times=10)
    h.submit(make_job(0))
    await h.run_for(400)
    assert h.state_of() == "STOPPING"
    h.submit(make_job(1))
    await h.run_for(2000)
    first, second = h.instances()
    assert first["destroy_requested_at"] is not None and first["state"] == "STOPPED"
    assert second["created_at"] >= first["destroy_accepted_at"]
    assert job_states(h) == [("completed", "cloud")] * 2


@pytest.mark.parametrize(
    ("fault", "times", "creates", "outcome"),
    [
        (Fault.CREATE_TIMEOUT_CREATED, 1, 1, "completed"),
        (Fault.CREATE_TIMEOUT_NOT_CREATED, 1, 2, "completed"),
        (Fault.CREATE_TIMEOUT_NOT_CREATED, 3, 2, "fallback"),
    ],
    ids=["adopt", "retry", "max"],
)
async def test_s06_create_unknown(
    tmp_path: Path, fault: Fault, times: int, creates: int, outcome: str
) -> None:
    h = Harness(tmp_path)
    h.provider.inject("create", fault, times=times)
    h.submit(make_job(0))
    await h.run_for(2000)
    assert h.provider.create_calls == creates
    job = h.jobs()[0]
    assert (job["status"] if outcome == "completed" else "fallback") == outcome
    if outcome == "fallback":
        assert job["route"] == "local:fallback"
    assert h.provider.live_refs() == []


async def test_s07_gpu_unavailable(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.provider.inject("create", Fault.GPU_UNAVAILABLE)
    h.submit(make_job(0))
    await h.run_for(1000)
    assert h.instances()[0]["host_id"] == "host-1" and h.jobs()[0]["status"] == "completed"
    d2 = tmp_path / "none"
    d2.mkdir()
    h2 = Harness(d2, knobs=MockKnobs(gpu_available=False))
    h2.submit(make_job(0))
    await h2.run_for(700)
    assert h2.jobs()[0]["fallback_reason"] == "gpu_unavailable"


async def test_s08_deploy_or_load_error(tmp_path: Path) -> None:
    h = Harness(tmp_path, inference_knobs=MockInferenceKnobs(test_fail=3))
    h.submit(make_job(0))
    await h.run_for(200)
    first = h.instances()[0]
    assert ("LOADING_MODEL", "ERROR", "model_test_failed") in h.transitions()
    bl = h.conn.execute("SELECT host_id FROM host_blacklist").fetchall()
    assert first["host_id"] in [r["host_id"] for r in bl]
    await h.run_for(2000)
    hosts = [r["host_id"] for r in h.instances()]
    assert len(set(hosts)) == len(hosts) == 3  # every cycle on another host
    job = h.jobs()[0]  # three failed cycles in a row -> local fallback
    assert (job["route"], job["fallback_reason"]) == ("local:fallback", "deployment_error")


async def test_s09_instance_crash_busy(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.submit(make_job(0, max_tokens=LONG))
    for _ in range(2):
        await h.run_for(150)
        while h.state_of() != "BUSY":
            await h.run_for(20)
        h.provider.interrupt(h.instances()[-1]["provider_ref"])
        await h.run_for(60)
    job = h.jobs()[0]
    assert job["attempts"] == 2 and job["route"] == "local:fallback"
    assert job["fallback_reason"].startswith("attempts_exhausted")


async def test_s10_loading_hang(tmp_path: Path) -> None:
    h = Harness(tmp_path, inference_knobs=MockInferenceKnobs(hang_load=True))
    h.submit(make_job(0))
    await h.run_for(1300)
    assert ("LOADING_MODEL", "ERROR", "loading_model_timeout") in h.transitions()


async def test_s11_gateway_instance_link_loss(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    t0 = h.clock.now()
    h.submit(make_job(0, max_tokens=78 * 60))
    await h.run_until(t0 + 80)
    ref = h.instances()[0]["provider_ref"]
    h.inference.lose_network(ref, until=t0 + 10**6)
    await h.run_until(t0 + 600)
    trail = h.transitions(h.instances()[0]["id"])
    assert ("BUSY", "ERROR", "network_loss") in trail or ("IDLE", "ERROR", "network_loss") in trail
    assert ref not in h.provider.live_refs()
    await h.run_until(t0 + 2000)
    job = h.jobs()[0]
    assert (job["status"], job["route"]) == ("completed", "cloud")
    assert h.provider.create_calls == 2


async def test_s12_client_link_loss(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    first = h.submit(make_job(0, idem_key="k"))
    await h.run_for(200)
    again = h.submit(make_job(0, idem_key="k"))  # the client reconnects and retries
    assert again.job_id == first.job_id and again.status == "completed"
    assert h.bodies.read_result(first.job_id) == b"ok"


async def test_s13_controller_restart(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    t0 = h.clock.now()
    leases.create_explicit(h.conn, holder="agent", client_id=CLIENT_ID, pool="p1", ttl_s=60,
                           now=t0)  # fmt: skip
    h.submit(make_job(0, max_tokens=LONG))
    await h.run_until(t0 + 150)
    assert h.state_of() == "BUSY"
    h.restart()
    await h.run_until(t0 + 160)
    job = h.jobs()[0]
    assert job["restarts"] == 1 and job["attempts"] == 1  # requeued, re-dispatched
    assert h.incidents("lease_expired")
    await h.run_until(t0 + 2000)
    assert job_states(h) == [("completed", "cloud")] and h.provider.create_calls == 1
    # READY/IDLE are adopted with a fresh idle timer; a valid-HMAC orphan is reported (Q1)
    d2 = tmp_path / "idle"
    d2.mkdir()
    h2 = Harness(d2)
    h2.submit(make_job(0))
    await h2.run_for(150)
    h2.clock.advance_to(h2.clock.now() + 200)
    h2.provider.add_orphan(make_label(new_op_id(), TEST_SECRET))
    h2.restart()
    await h2.step()
    assert h2.instances()[0]["idle_since"] == h2.clock.now()
    assert h2.incidents("unknown_owned_resource") and is_halted(h2.conn)


async def test_s14_long_agent_task_lease(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    t0 = h.clock.now()
    lease = leases.create_explicit(h.conn, holder="agent", client_id=CLIENT_ID, pool="p1",
                                   ttl_s=600, now=t0)  # fmt: skip
    h.submit(make_job(0))
    for k in range(1, 7):
        await h.run_until(t0 + 200 * k)
        leases.renew(h.conn, lease, h.clock.now())
    assert h.state_of() == "BUSY" and h.provider.create_calls == 1


async def test_s15_stale_leases(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    t0 = h.clock.now()
    leases.create_explicit(h.conn, holder="agent", client_id=CLIENT_ID, pool="p1", ttl_s=30,
                           now=t0)  # fmt: skip
    await h.run_until(t0 + 31)
    inc = h.incidents("lease_expired")
    assert len(inc) == 1 and inc[0]["severity"] == "info"
    with pytest.raises(LeaseError):
        leases.create_hold(h.conn, holder="admin", pool="p1", ttl_s=8 * 3600 + 1, now=t0)


async def test_s16_duplicate_request(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    a = h.submit(make_job(0, idem_key="key-1"))
    b = h.submit(make_job(0, idem_key="key-1"))
    c = h.submit(make_job(7))
    d = h.submit(make_job(7))  # implicit dedup: same client + body while queued
    assert a.job_id == b.job_id and c.job_id == d.job_id and not b.created and not d.created
    await h.run_for(1500)
    assert len(h.jobs()) == 2 and h.provider.create_calls == 1


async def test_s17_db_corruption(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.conn.close()
    h.db_path.write_bytes(b"garbage" * 1000)
    opened = open_state_db(h.db_path, h.clock)
    assert opened.safe_mode
    h.conn = opened.conn
    h.add_client("client-1")
    h.controller = Controller(h.conn, h.cfg, h.clock, {"mock": h.provider},
                              {"mock": h.inference}, h.bodies, TEST_SECRET, "boot-x")  # fmt: skip
    h.submit(make_job(0))
    await h.run_for(300)
    assert h.provider.create_calls == 0 and is_halted(h.conn)
    assert h.jobs()[0]["route"] == "local:fallback"


async def test_s18_destroy_429_error(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.provider.inject("destroy", Fault.DESTROY_429, times=1000)
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 2000)
    assert ("STOPPING", "PENDING_DELETE", "stop_confirm_timeout") in h.transitions()
    assert h.state_of() == "LEAKED"
    assert h.incidents("leak")[0]["severity"] == "critical"
    assert is_blocked(h.conn, "mock") == "leak" and is_halted(h.conn)
    circuit = h.conn.execute("SELECT circuit_state FROM provider_accounts").fetchone()[0]
    assert circuit == "closed"  # destroy errors never open the breaker
    nexts = [r["next_destroy_at"] for r in h.instances()]
    assert nexts[0] is not None


@pytest.mark.parametrize("cause", ["insufficient_balance", "quota_exceeded", "budget_limit_reached"])
async def test_s19_funds_quota_budget(tmp_path: Path, cause: str) -> None:
    h = Harness(tmp_path)
    if cause == "insufficient_balance":
        h.provider.inject("create", Fault.INSUFFICIENT_BALANCE)
    elif cause == "quota_exceeded":
        h.provider.inject("create", Fault.QUOTA)
    else:
        reservations.reserve(h.conn, "session", "external", 45.0, h.clock.now())
    h.submit(make_job(0))
    await h.run_for(300)
    job = h.jobs()[0]
    assert (job["route"], job["fallback_reason"]) == ("local:fallback", cause)
    if cause != "budget_limit_reached":
        assert is_blocked(h.conn, "mock") == cause
