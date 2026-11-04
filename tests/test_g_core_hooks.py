"""T-369 G4: additive core edits."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from d_fixtures import T0, Env, spec
from harness import Harness
from llmhub.cloud_manager.inference import JobRunResult
from llmhub.task_queue import queue

RES = JobRunResult(tokens_in=1, tokens_out=2, finish_reason="stop", body=b"{}")


def test_complete_cancel_requested_is_cancelled(tmp_path: Path) -> None:
    env = Env(tmp_path)
    jid = queue.submit(env.conn, env.bodies, spec(1), T0, env.cfg).job_id
    queue.mark_running(env.conn, jid, "i1", "w", T0)
    queue.cancel(env.conn, jid, T0 + 1)
    queue.complete(env.conn, jid, RES, 0.07, T0 + 2, "r")
    j = env.job(jid)
    assert (j["status"], j["cost_usd"], j["res_path"]) == ("cancelled", 0.07, "r")
    assert env.lease(jid)["closed_at"] == T0 + 2


def test_complete_normal_unchanged(tmp_path: Path) -> None:
    env = Env(tmp_path)
    jid = queue.submit(env.conn, env.bodies, spec(1), T0, env.cfg).job_id
    queue.mark_running(env.conn, jid, "i1", "w", T0)
    queue.complete(env.conn, jid, RES, 0.07, T0 + 2, "r")
    assert env.job(jid)["status"] == "completed"


def test_est_cost_usd_written(tmp_path: Path) -> None:
    env = Env(tmp_path)
    a = queue.submit(env.conn, env.bodies, replace(spec(1), est_cost_usd=0.12), T0, env.cfg)
    b = queue.submit(env.conn, env.bodies, spec(2), T0, env.cfg)
    assert env.job(a.job_id)["est_cost_usd"] == 0.12
    assert env.job(b.job_id)["est_cost_usd"] is None


def test_requeue_local_job_max_restarts_fails(tmp_path: Path) -> None:
    env = Env(tmp_path)
    s = replace(spec(1), route="local", pool=None)
    jid = queue.submit(env.conn, env.bodies, s, T0, env.cfg).job_id
    env.conn.execute("UPDATE jobs SET restarts=?, status='running' WHERE id=?",
                     (queue.MAX_RESTARTS + 1, jid))  # fmt: skip
    assert queue.requeue(env.conn, jid, T0 + 1, count_attempt=False, reason="restart") == "failed"
    j = env.job(jid)
    assert (j["status"], j["error_code"], j["fallback_reason"]) == (
        "failed", "local_unavailable", "max_restarts",
    )  # fmt: skip


def test_requeue_cloud_job_max_restarts_still_falls_back(tmp_path: Path) -> None:
    env = Env(tmp_path)
    jid = queue.submit(env.conn, env.bodies, spec(1), T0, env.cfg).job_id
    queue.mark_running(env.conn, jid, "i1", "w", T0)
    env.conn.execute("UPDATE jobs SET restarts=? WHERE id=?", (queue.MAX_RESTARTS + 1, jid))
    assert queue.requeue(env.conn, jid, T0 + 1, count_attempt=False, reason="r") == "fallback"
    assert env.job(jid)["route"] == "local:fallback"


async def test_controller_wake(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    c = h.controller
    c.clear_wake()
    assert not c.wake_pending()
    c.wake()
    assert c.wake_pending()
    stop = asyncio.Event()
    task = asyncio.ensure_future(c.run_forever(stop))
    await asyncio.sleep(0)
    for _ in range(5):
        await asyncio.sleep(0)
    c.wake()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not c.wake_pending()  # run_forever consumed the wake-up
    stop.set()
    c.wake()
    await asyncio.wait_for(task, 5)
