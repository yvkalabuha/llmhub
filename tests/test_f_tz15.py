"""F6: scenarios of TZ section 15 and the task criteria (plan section 10.1)."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from f_fixtures import cfg_with, count, edges, job_states
from harness import CLIENT_ID, TEST_SECRET, Harness, make_job
from llmhub.budget_manager import reservations
from llmhub.budget_manager.limits import windows
from llmhub.clock import FakeClock
from llmhub.controller import Controller
from llmhub.cost_tracker.ledger import session_cost, spent
from llmhub.lifecycle_manager import leases
from llmhub.lifecycle_manager.safety import halt
from llmhub.persistence.db import connect
from llmhub.persistence.integrity import open_state_db
from llmhub.persistence.state import ReconcileRequired
from llmhub.provider_adapters.labels import make_label
from llmhub.provider_adapters.mock import Fault, MockKnobs, MockProvider
from llmhub.provider_adapters.mock_inference import MockInference
from llmhub.provider_adapters.mock_store import MockStore
from llmhub.task_queue.models import IdempotencyConflict, Priority

LONG = 78 * 600


async def test_10_concurrent_requests_one_create(tmp_path: Path) -> None:
    store = tmp_path / "mock_cloud.db"
    h = Harness(tmp_path, store_path=store)
    async def submit(i: int) -> str:
        await asyncio.sleep(0)
        return h.submit(make_job(i)).job_id

    assert len(set(await asyncio.gather(*(submit(i) for i in range(10))))) == 10
    t0 = h.clock.now()
    providers: list[MockProvider] = []
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            conn = connect(h.db_path, busy_timeout_ms=300)
            clock = FakeClock(t0)
            prov = MockProvider(MockStore(store), clock)
            providers.append(prov)
            ctl = Controller(conn, h.cfg, clock, {"mock": prov},
                             {"mock": MockInference(prov, clock)}, h.bodies, TEST_SECRET,
                             boot_id=f"thread-{n}")  # fmt: skip

            async def go() -> None:
                # R1-12: no step limit (Busy only postpones a step) and never leave
                # asyncio.run with a background create in flight (that would cancel it and
                # let the harness controller create again); a wall deadline bounds the test
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    await ctl.step()
                    while ctl.busy_ops():
                        await asyncio.sleep(0.01)
                    ref = conn.execute(
                        "SELECT 1 FROM instances WHERE provider_ref IS NOT NULL"
                    ).fetchone()
                    if ref is not None:
                        return
                    await asyncio.sleep(0.01)
                raise AssertionError("no instance got a provider_ref")

            asyncio.run(go())
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert sum(p.create_calls for p in providers) == 1, [p.create_calls for p in providers]
    assert count(h, "SELECT count(*) FROM instances") == 1
    h.restart()
    await h.run_for(3000)
    assert job_states(h) == [("completed", "cloud")] * 10
    assert h.provider.create_calls == 0, h.transitions()  # the threads' instance served all


async def test_several_agents_urgent_skips_batch(tmp_path: Path) -> None:
    h = Harness(tmp_path, clients=("a", "b", "c"))
    t0 = h.clock.now()
    for i, c in enumerate(["a", "a", "b", "c"]):
        h.submit(make_job(i, priority=Priority.LOW, client=c))
    await h.run_for(600)
    assert h.instances() == []  # 4 low < batch of 5, wait up to 30 min
    h.submit(make_job(9, priority=Priority.URGENT, client="c"))
    await h.run_until(t0 + 3000)
    assert h.provider.create_calls == 1
    order = [(j["priority"], j["client_id"]) for j in
             sorted(h.jobs(), key=lambda j: j["started_at"])]  # fmt: skip
    assert order[0] == ("urgent", "c")
    assert [c for _, c in order[1:]] == ["a", "b", "a", "c"] or \
        [c for _, c in order[1:]][:2] in (["a", "b"], ["b", "a"])  # round-robin, not a,a,...
    assert all(s == ("completed", "cloud") for s in job_states(h))


async def test_retries_same_idempotency_key_one_job(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    ids = {h.submit(make_job(0, idem_key="k1")).job_id for _ in range(5)}
    assert len(ids) == 1
    with pytest.raises(IdempotencyConflict):
        h.submit(make_job(1, idem_key="k1"))  # other body -> 409 at the API
    await h.run_for(1500)
    assert job_states(h) == [("completed", "cloud")]
    again = h.submit(make_job(0, idem_key="k1"))
    assert again.created is False and again.status == "completed"


@pytest.mark.parametrize("branch", ["before_create", "during_create"])
async def test_restart_in_provisioning_no_extra_resources(tmp_path: Path, branch: str) -> None:
    h = Harness(tmp_path, knobs=MockKnobs(hang_s=30))
    t0 = h.clock.now()
    h.submit(make_job(0))
    if branch == "before_create":
        await h.step()  # E1 only: the row is PROVISIONING without a create intent
        assert h.instances()[0]["create_called_at"] is None
    else:
        h.provider.inject("create", Fault.CREATE_HANG)
        await h.run_until(t0 + 5)
        inst = h.instances()[0]
        assert inst["create_called_at"] is not None and inst["provider_ref"] is None
        assert len(h.provider.live_refs()) == 1
    h.restart()
    await h.run_until(t0 + 2000)
    assert job_states(h) == [("completed", "cloud")]
    assert count(h, "SELECT count(*) FROM instances") == 1
    assert len(h.provider.store.conn.execute("SELECT * FROM resources").fetchall()) == 1
    assert h.provider.live_refs() == []


async def test_restart_in_busy_no_loss(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    t0 = h.clock.now()
    h.submit(make_job(0, max_tokens=LONG))
    for k in range(3):  # three restarts in a row while the job runs
        await h.run_until(t0 + 200 + 50 * k)
        assert h.state_of() == "BUSY"
        h.restart()
    await h.run_until(t0 + 3000)
    job = h.jobs()[0]
    assert (job["status"], job["route"]) == ("completed", "cloud")
    assert (job["restarts"], job["attempts"]) == (3, 1)
    assert h.provider.create_calls == 1  # the resource was adopted, not re-created


async def test_gpu_interruption_requeue(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    t0 = h.clock.now()
    h.submit(make_job(0, max_tokens=LONG))
    await h.run_until(t0 + 200)
    h.provider.interrupt(h.instances()[0]["provider_ref"])
    await h.run_until(t0 + 240)
    job = h.jobs()[0]
    assert (job["status"], job["attempts"]) == ("queued", 1)
    await h.run_until(t0 + 3000)
    job = h.jobs()[0]
    assert (job["status"], job["route"], job["attempts"]) == ("completed", "cloud", 2)
    assert h.provider.create_calls == 2


async def _stopping_with_failing_destroy(h: Harness) -> float:
    t0 = h.clock.now()
    h.provider.inject("destroy", Fault.DESTROY_FAIL, times=6)
    h.submit(make_job(0))
    await h.run_until(t0 + 400)  # job done ~+81, idle 300 -> STOPPING, destroy keeps failing
    assert h.state_of() == "STOPPING"
    return t0


async def test_request_during_stopping_new_cycle(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    t0 = await _stopping_with_failing_destroy(h)
    h.submit(make_job(1))
    await h.run_until(t0 + 3000)
    rows = h.instances()
    assert len(rows) == 2 and all(r["state"] == "STOPPED" for r in rows)
    first_stop = h.conn.execute(
        "SELECT ts FROM transitions WHERE instance_id=? AND to_state='PENDING_DELETE'",
        (rows[0]["id"],),
    ).fetchone()[0]
    assert rows[1]["created_at"] >= first_stop  # the new cycle starts after E19 only
    assert job_states(h) == [("completed", "cloud")] * 2
    for r in rows:
        assert count(h, "SELECT count(*) FROM ledger WHERE instance_id=?", r["id"]) > 0
    assert h.provider.live_refs() == []


async def test_new_cycle_while_pending_delete(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    t0 = await _stopping_with_failing_destroy(h)
    await h.run_until(t0 + 600)  # stop_confirm_s 180 -> PENDING_DELETE without acceptance
    old = h.instances()[0]
    assert old["state"] == "PENDING_DELETE"
    h.submit(make_job(1))
    await h.run_until(t0 + 640)
    live = count(h, "SELECT count(*) FROM instances WHERE state NOT IN"
                    " ('STOPPED','PENDING_DELETE','LEAKED')")  # fmt: skip
    assert live == 1 and count(h, "SELECT count(*) FROM instances") == 2
    await h.run_until(t0 + 3000)
    assert [r["state"] for r in h.instances()] == ["STOPPED", "STOPPED"]


async def test_budget_exhaustion_no_launch_local_fallback(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    reservations.reserve(h.conn, "session", "external", 3.0, h.clock.now())  # day = 100 %
    h.submit(make_job(0))
    await h.run_for(300)
    assert h.provider.create_calls == 0 and h.instances() == []
    job = h.jobs()[0]
    assert (job["route"], job["fallback_reason"]) == ("local:fallback", "budget_limit_reached")


async def test_balance_exhaustion_block_manual_resume(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.provider.inject("create", Fault.INSUFFICIENT_BALANCE)
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 100)
    h.provider.knobs.balance_usd = 500
    h.submit(make_job(1))
    await h.run_until(t0 + 1000)
    assert h.provider.create_calls == 1
    h.controller.resume()
    h.submit(make_job(2))
    await h.run_until(t0 + 3000)
    assert h.provider.create_calls == 2 and h.jobs()[-1]["status"] == "completed"


async def test_no_extra_launches(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    t0 = h.clock.now()
    for i in range(30):  # a stream while the GPU lives
        h.arrive_at(t0 + 20 * i, make_job(i))
    await h.run_until(t0 + 3000)
    assert h.provider.create_calls == 1
    h2_dir = tmp_path / "bursts"
    h2_dir.mkdir()
    h2 = Harness(h2_dir)
    t1 = h2.clock.now()
    for burst in range(3):  # S2-like: 5 requests within 10 min, every 2 h
        for k in range(5):
            h2.arrive_at(t1 + 7200 * burst + 100 * k, make_job(100 * burst + k))
    await h2.run_until(t1 + 4 * 7200)
    assert h2.provider.create_calls == 3
    assert all(s == ("completed", "cloud") for s in job_states(h2))


async def test_idle_shutdown_after_timeout(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 120)
    idle_since = h.instances()[0]["idle_since"]
    assert h.state_of() == "IDLE"
    h.arrive_at(idle_since + 299, make_job(1))
    await h.run_until(idle_since + 320)
    assert h.provider.create_calls == 1 and h.state_of() == "IDLE"
    assert ("IDLE", "BUSY") in edges(h)
    second_idle = h.instances()[0]["idle_since"]
    await h.run_until(second_idle + 299)
    assert h.state_of() == "IDLE"
    await h.run_until(second_idle + 301)
    assert h.state_of() == "STOPPED"
    stop = h.conn.execute("SELECT ts FROM transitions WHERE to_state='STOPPING'").fetchone()[0]
    assert stop == second_idle + 300


@pytest.mark.parametrize("idle_s", [0, 900])
async def test_idle_timeout_zero_and_15(tmp_path: Path, idle_s: int) -> None:
    h = Harness(tmp_path, cfg=cfg_with(tmp_path, pool={"idle_timeout_s": idle_s}))
    h.submit(make_job(0))
    await h.run_for(2000)
    idle_at = h.conn.execute("SELECT ts FROM transitions WHERE to_state='IDLE'").fetchone()[0]
    stop_at = h.conn.execute("SELECT ts FROM transitions WHERE to_state='STOPPING'").fetchone()[0]
    assert stop_at - idle_at == idle_s


async def test_drain_after_batch(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    t0 = h.clock.now()
    lease = leases.create_explicit(h.conn, holder="agent", client_id=CLIENT_ID, pool="p1",
                                   now=t0)  # fmt: skip
    for i in range(3):
        h.submit(make_job(i))
    await h.run_until(t0 + 200)
    assert h.state_of() == "BUSY"  # all done, the lease keeps the GPU
    leases.close(h.conn, lease, h.clock.now(), final=True)
    closed = h.clock.now()
    await h.run_until(t0 + 260)
    stop = h.conn.execute(
        "SELECT ts, reason FROM transitions WHERE to_state='STOPPING'"
    ).fetchone()
    assert (stop["ts"], stop["reason"]) == (closed, "drain")


async def test_agent_silent_20min_with_lease_gpu_alive(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    t0 = h.clock.now()
    lease = leases.create_explicit(h.conn, holder="agent", client_id=CLIENT_ID, pool="p1",
                                   now=t0)  # fmt: skip
    h.submit(make_job(0))
    for k in range(1, 7):  # 20 min of silence, renew every 200 s
        await h.run_until(t0 + 200 * k)
        leases.renew(h.conn, lease, h.clock.now())
    await h.run_until(t0 + 1300)
    assert h.state_of() in ("BUSY", "READY")
    assert ("BUSY", "IDLE") not in edges(h)
    leases.close(h.conn, lease, h.clock.now())
    await h.run_until(t0 + 1300 + 400)
    assert h.state_of() == "STOPPED"


async def test_lease_expired_without_heartbeat(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    t0 = h.clock.now()
    leases.create_explicit(h.conn, holder="agent", client_id=CLIENT_ID, pool="p1", now=t0)
    h.submit(make_job(0))
    await h.run_until(t0 + 599)
    assert h.state_of() == "BUSY"
    await h.run_until(t0 + 601)
    assert h.state_of() == "IDLE" and h.incidents("lease_expired")


@pytest.mark.parametrize(
    "fault",
    [Fault.CREATE_TIMEOUT_CREATED, Fault.CREATE_TIMEOUT_NOT_CREATED,
     Fault.CREATE_TIMEOUT_CREATED_LATE],
)  # fmt: skip
async def test_create_unknown_outcome_one_resource(tmp_path: Path, fault: Fault) -> None:
    h = Harness(tmp_path)
    h.provider.inject("create", fault)
    t0 = h.clock.now()
    h.submit(make_job(0, max_tokens=LONG))  # long enough to outlive the late duplicate
    await h.run_until(t0 + 450)
    assert len(h.provider.live_refs()) == 1
    assert h.provider.live_refs() == [h.instances()[0]["provider_ref"]]


async def test_db_corruption_safe_mode(tmp_path: Path) -> None:
    store = tmp_path / "mock_cloud.db"
    h = Harness(tmp_path, store_path=store)
    t0 = h.clock.now()
    h.submit(make_job(0, max_tokens=LONG))
    await h.run_until(t0 + 200)
    foreign = h.provider.add_foreign("someone-else-1")
    forged = h.provider.add_foreign(make_label("0123456789ab", b"x" * 40))  # bad HMAC
    h.submit(make_job(1))
    assert len(h.provider.live_refs()) == 3
    h.restart()  # the process dies with the database
    h.conn.close()
    with h.db_path.open("r+b") as f:
        f.write(b"\x00" * 100)  # header destroyed
    for side in ("-wal", "-shm"):
        h.db_path.with_name(h.db_path.name + side).unlink(missing_ok=True)
    opened = open_state_db(h.db_path, h.clock)
    assert opened.safe_mode and opened.reason == "db_corrupt"
    h.conn = opened.conn
    h.controller = h._make_controller()
    rep = await h.drive(h.reaper().run_once())
    assert rep.sweep.destroyed and h.provider.live_refs() == sorted([foreign, forged])
    await h.run_until(t0 + 600)
    assert h.provider.create_calls == 1  # no new create in safe mode
    restored = h.jobs()
    assert len(restored) == 2  # both bodies had no result: re-queued
    assert all(j["route"] == "local:fallback" for j in restored)
    with pytest.raises(ReconcileRequired):
        h.controller.resume()
    await h.drive(h.controller.reload_billing())
    rows = count(h, "SELECT count(*) FROM ledger WHERE source='provider'")
    assert rows > 0
    w = windows(h.clock.now())
    assert spent(h.conn, w.month_start, w.month_end) > 0  # the month budget did not reset
    h.controller.resume()
    assert h.conn.execute("SELECT value FROM controller_state WHERE key='halted'").fetchone()[0] == "0"


async def test_blocked_job_doesnt_hold_gpu(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 120)
    assert h.state_of() == "IDLE"
    big = h.submit(make_job(1, max_tokens=78 * 5000))  # work > budget_job on the live GPU
    await h.run_until(t0 + 130)
    job = h.conn.execute("SELECT * FROM jobs WHERE id=?", (big.job_id,)).fetchone()
    assert (job["route"], job["fallback_reason"]) == ("local:fallback", "budget_limit_reached")
    lease = h.conn.execute("SELECT closed_at FROM leases WHERE job_id=?", (big.job_id,)).fetchone()
    assert lease["closed_at"] is not None
    await h.run_until(t0 + 700)
    assert h.state_of() == "STOPPED"


async def test_insert_provisioning_gate_in_tx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    other = connect(h.db_path)
    import llmhub.controller as ctl

    original = ctl.launch_trigger

    def racing(*a: object, **kw: object):  # type: ignore[no-untyped-def]
        decision = original(*a, **kw)  # type: ignore[arg-type]
        halt(other, "admin", h.clock.now())  # another connection halts between decision/insert
        return decision

    monkeypatch.setattr(ctl, "launch_trigger", racing)
    h.submit(make_job(0))
    await h.run_for(60)
    assert h.instances() == [] and h.provider.create_calls == 0


async def test_max_lifetime_stop_after_current(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    t0 = h.clock.now()
    for k in range(42):  # keep the instance alive until the long job
        h.arrive_at(t0 + 250 * k, make_job(100 + k))
    h.arrive_at(t0 + 10500, make_job(1, max_tokens=LONG))
    await h.run_until(t0 + 10800 + 10)
    inst = h.instances()[0]
    assert inst["state"] == "BUSY" and inst["stop_after_current"] == 1
    await h.run_until(t0 + 10800 + 600)
    assert ("BUSY", "STOPPING", "E16") in h.transitions()
    # hold extends max_lifetime to its TTL
    d2 = tmp_path / "hold"
    d2.mkdir()
    h2 = Harness(d2)
    t1 = h2.clock.now()
    h2.submit(make_job(0))
    await h2.run_until(t1 + 100)
    h2.controller.hold("p1", 4 * 3600)
    await h2.run_until(t1 + 10800 + 600)
    assert h2.state_of() in ("IDLE", "BUSY", "READY")
    assert h2.instances()[0]["max_lifetime_at"] >= t1 + 100 + 4 * 3600


async def test_budget_session_stop_after_current(tmp_path: Path) -> None:
    cfg = cfg_with(tmp_path, pool={"max_price_usd_h": 2.0}, budget={"budget_job": 1.0})
    h = Harness(tmp_path, cfg=cfg, knobs=MockKnobs(price_usd_h=2.0))
    t0 = h.clock.now()
    for i in range(8):
        h.arrive_at(t0 + 60 * i, make_job(i, max_tokens=78 * 300))
    await h.run_until(t0 + 6 * 3600)
    rows = h.instances()
    assert rows[0]["stop_reason"] == "budget_session"
    assert len(rows) >= 2  # the rest ran in a new session
    assert all(s == ("completed", "cloud") for s in job_states(h))
    for r in rows:
        assert session_cost(h.conn, r["id"]) <= cfg.budget.budget_session * 1.05


async def test_ledger_vs_mock_billing_within_tolerance(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    t0 = h.clock.now()
    for i in range(24):  # day of S1-like traffic: one request per hour
        h.arrive_at(t0 + 3600 * i + 600, make_job(i))
    await h.run_until(t0 + 86400)
    ours = h.ledger_total()
    billed = h.provider.billed_total()
    assert abs(ours - billed) <= max(0.05, 0.05 * billed), (ours, billed)
    assert h.provider.create_calls == 24
