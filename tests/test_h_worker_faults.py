"""T-369 H5: LocalWorker faults, cancel, heartbeat, restart, expiry."""

from __future__ import annotations

import json
from pathlib import Path

from h_fixtures import LocalEnv, lspec
from llmhub.task_queue import queue


async def test_5xx_requeued_then_succeeds(tmp_path: Path) -> None:
    env = LocalEnv(tmp_path)
    env.fakes["cpu"].chat_actions.append(503)
    jid = env.submit(lspec(1))
    await env.run_for(10)
    r = env.job(jid)
    assert r["status"] == "completed" and r["attempts"] == 2 and r["error_code"] is None
    assert len(env.fakes["cpu"].chat_requests) == 2


async def test_two_failures_fail_local_unavailable(tmp_path: Path) -> None:
    env = LocalEnv(tmp_path)
    env.fakes["cpu"].chat_actions.extend([500, "drop"])
    jid = env.submit(lspec(1))
    await env.run_for(10)
    r = env.job(jid)
    assert (r["status"], r["error_code"], r["attempts"]) == ("failed", "local_unavailable", 2)
    assert r["route"] == "local" and r["pool"] is None  # never goes to the cloud
    assert env.notified.count(jid) >= 2


async def test_timeouts_keep_last_code(tmp_path: Path) -> None:
    env = LocalEnv(tmp_path)
    env.fakes["cpu"].delays.extend([500, 500])
    jid = env.submit(lspec(1, timeout_s=30.0))
    await env.run_for(100)
    r = env.job(jid)
    assert (r["status"], r["error_code"]) == ("failed", "upstream_timeout")
    assert env.fakes["cpu"].aborted == 2


async def test_context_too_large_no_retry(tmp_path: Path) -> None:
    env = LocalEnv(tmp_path)
    env.fakes["cpu"].chat_actions.append("context")
    jid = env.submit(lspec(1))
    await env.run_for(10)
    r = env.job(jid)
    assert (r["status"], r["error_code"], r["attempts"]) == ("failed", "context_too_large", 1)
    assert len(env.fakes["cpu"].chat_requests) == 1


async def test_alias_without_local_backend(tmp_path: Path) -> None:
    env = LocalEnv(tmp_path)
    fb = env.submit(lspec(1, alias="cloudonly", route="local:fallback"))
    env.conn.execute("UPDATE jobs SET fallback_reason='budget_stage' WHERE id=?", (fb,))
    plain = env.submit(lspec(2, alias="cloudonly", route="local"))
    await env.run_for(5)
    r = env.job(fb)
    assert (r["status"], r["error_code"], r["fallback_reason"]) == (
        "failed", "cloud_unavailable", "budget_stage")  # fmt: skip
    assert env.job(plain)["error_code"] == "local_unavailable"
    assert all(f.requests == [] for f in env.fakes.values())
    assert set(env.notified) == {fb, plain}


async def test_cancel_running_cuts_upstream(tmp_path: Path) -> None:
    env = LocalEnv(tmp_path)
    env.fakes["cpu"].delays.append(1000)
    jid = env.submit(lspec(1))
    await env.run_for(5)
    assert env.job(jid)["status"] == "running"
    queue.cancel(env.conn, jid, env.clock.now())  # API path: sets cancel_requested
    await env.run_for(5)
    r = env.job(jid)
    assert r["status"] == "cancelled" and env.fakes["cpu"].aborted == 1
    assert r["worker_id"] is None and jid in env.notified
    nxt = env.submit(lspec(2))  # slot is free again
    await env.run_for(5)
    assert env.job(nxt)["status"] == "completed"


async def test_direct_cancel_method(tmp_path: Path) -> None:
    env = LocalEnv(tmp_path)
    env.fakes["cpu"].delays.append(1000)
    jid = env.submit(lspec(1))
    await env.run_for(5)
    assert env.worker.cancel(jid) and not env.worker.cancel("nope")
    await env.run_for(2)
    assert env.job(jid)["status"] == "cancelled" and env.fakes["cpu"].aborted == 1


async def test_ten_minute_job_not_stolen_by_controller(tmp_path: Path) -> None:
    env = LocalEnv(tmp_path)
    env.fakes["cpu"].delays.append(600)
    jid = env.submit(lspec(1))
    await env.run_for(595)  # many Controller.step() passes, stale limit is 180 s
    r = env.job(jid)
    assert (r["status"], r["restarts"], r["attempts"]) == ("running", 0, 1)
    assert env.clock.now() - r["worker_heartbeat_at"] <= env.cfg.timeouts.worker_heartbeat_s
    await env.run_for(20)
    r = env.job(jid)
    assert r["status"] == "completed" and r["restarts"] == 0
    assert len(env.fakes["cpu"].chat_requests) == 1


async def test_dead_worker_job_is_requeued_by_stale_heartbeat(tmp_path: Path) -> None:
    env = LocalEnv(tmp_path)
    env.fakes["cpu"].delays.append(1000)
    jid = env.submit(lspec(1))
    await env.run_for(5)
    for t in env.worker._tasks.values():
        t.cancel()  # worker dies silently, no more heartbeats
    await env.run_for(200, controller=True)
    r = env.job(jid)  # the stale heartbeat returned it; the second run is instant
    assert r["status"] == "completed" and r["restarts"] == 1


async def test_restart_requeues_and_reruns(tmp_path: Path) -> None:
    env = LocalEnv(tmp_path)
    env.fakes["cpu"].delays.append(1000)
    jid = env.submit(lspec(1))
    await env.run_for(5)
    old_boot = env.job(jid)["worker_id"]
    env.restart()
    await env.run_for(10)
    r = env.job(jid)
    assert r["status"] == "completed" and r["restarts"] == 1 and r["attempts"] == 1
    assert r["worker_id"] != old_boot and r["worker_id"] == env.h.controller.boot_id
    assert len(env.fakes["cpu"].chat_requests) == 2


async def test_late_answer_of_old_call_does_not_overwrite(tmp_path: Path) -> None:
    env = LocalEnv(tmp_path)
    env.fakes["cpu"].delays.extend([100, 0])
    env.fakes["cpu"].text = "from-old"
    jid = env.submit(lspec(1))
    await env.run_for(5)
    old = env.worker
    old_tasks = list(old._tasks.values())
    # the "crash" keeps the old call alive on purpose: it answers late
    env.h.restart()
    env.worker = env.make_worker()
    env.fakes["cpu"].text = "from-new"
    await env.run_for(20)
    assert env.job(jid)["status"] == "completed"
    assert (
        json.loads(env.h.bodies.read_result(jid))["choices"][0]["message"]["content"] == "from-new"
    )
    before = dict(env.job(jid))
    await env.run_for(150)  # the old call finishes here
    assert all(t.done() for t in old_tasks)
    assert dict(env.job(jid)) == before
    assert (
        json.loads(env.h.bodies.read_result(jid))["choices"][0]["message"]["content"] == "from-new"
    )
    assert env.notified.count(jid) == 1


async def test_step_expires_overdue_without_calling_backend(tmp_path: Path) -> None:
    env = LocalEnv(tmp_path)
    now = env.clock.now()
    dl = env.submit(lspec(1, deadline_at=now + 5))
    qw = env.submit(lspec(2, max_queue_wait_s=7.0))
    env.clock.advance_to(now + 10)
    await env.worker.step()
    assert (env.job(dl)["status"], env.job(dl)["error_code"]) == ("failed", "deadline_exceeded")
    assert env.job(qw)["error_code"] == "queue_wait_exceeded"
    assert env.fakes["cpu"].requests == [] and {dl, qw} <= set(env.notified)


async def test_queued_job_behind_running_expires(tmp_path: Path) -> None:
    env = LocalEnv(tmp_path)
    env.fakes["cpu"].delays.append(100)
    env.submit(lspec(1))
    waiting = env.submit(lspec(2, max_queue_wait_s=20.0))
    await env.run_for(30)
    assert env.job(waiting)["error_code"] == "queue_wait_exceeded"
    assert len(env.fakes["cpu"].chat_requests) == 1 or env.fakes["cpu"].in_flight == 1
