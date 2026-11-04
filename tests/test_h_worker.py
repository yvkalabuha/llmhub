"""T-369 H4: LocalWorker happy paths (FakeClock, ASGI fake)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from h_fixtures import LocalEnv, lspec
from llmhub.task_queue.models import Priority


async def test_one_job_completes(tmp_path: Path) -> None:
    env = LocalEnv(tmp_path)
    jid = env.submit(lspec(1))
    await env.run_for(10)
    r = env.job(jid)
    assert r["status"] == "completed" and r["backend"] == "local:cpu"
    assert (r["tokens_in"], r["tokens_out"], r["finish_reason"]) == (100, 40, "stop")
    assert r["cost_usd"] == 0.0 and r["worker_id"] == env.h.controller.boot_id
    assert json.loads(env.h.bodies.read_result(jid))["object"] == "chat.completion"
    assert r["res_path"].endswith(f"{jid}.res") and r["error_code"] is None
    assert env.notified == [jid]
    sent = env.fakes["cpu"].chat_requests[0].json
    assert sent["model"] == "model-cpu" and "_llmhub" not in sent and sent["stream"] is False
    assert env.worker.stats["cpu"].tg.value != 5  # EWMA moved (timings 4 tps)


async def test_semaphore_one_across_backends(tmp_path: Path) -> None:
    env = LocalEnv(tmp_path)
    for f in env.fakes.values():
        f.delays.append(100)
    a = env.submit(lspec(1, alias="m1"))
    b = env.submit(lspec(2, alias="m2"))
    await env.run_for(50)
    assert env.job(a)["status"] == "running" and env.job(b)["status"] == "queued"
    assert env.fakes["cpu2"].requests == []
    await env.run_for(70)
    assert env.job(a)["status"] == "completed"
    assert env.job(b)["status"] == "running" or env.job(b)["status"] == "completed"
    await env.run_for(200)
    assert env.job(b)["status"] == "completed" and env.job(b)["backend"] == "local:cpu2"
    assert all(f.max_in_flight == 1 for f in env.fakes.values())
    assert env.job(b)["started_at"] >= env.job(a)["finished_at"]


async def test_urgent_overtakes_low(tmp_path: Path) -> None:
    env = LocalEnv(tmp_path)
    env.fakes["cpu"].delays.append(50)
    first = env.submit(lspec(0))
    await env.run_for(5)  # `first` occupies the slot
    low = env.submit(lspec(1, prio=Priority.LOW))
    urgent = env.submit(lspec(2, prio=Priority.URGENT))
    await env.run_for(300)
    assert env.job(first)["status"] == env.job(low)["status"] == "completed"
    assert env.job(urgent)["started_at"] < env.job(low)["started_at"]


async def test_enable_thinking_and_max_tokens(tmp_path: Path) -> None:
    env = LocalEnv(tmp_path)

    def pl(tag: str) -> dict[str, object]:
        return {"messages": [{"role": "user", "content": tag}]}

    plain = pl("a")
    env.submit(lspec(1, task_type="think", body=plain))
    await env.run_for(5)
    s = env.fakes["cpu"].chat_requests[-1].json
    assert s["chat_template_kwargs"] == {"enable_thinking": True} and s["max_tokens"] == 300

    own = {**pl("b"), "chat_template_kwargs": {"enable_thinking": False}, "max_tokens": 9}
    env.submit(lspec(2, task_type="think", body=own))
    await env.run_for(5)
    s = env.fakes["cpu"].chat_requests[-1].json
    assert s["chat_template_kwargs"]["enable_thinking"] is False and s["max_tokens"] == 9

    env.submit(lspec(3, task_type="plain", body=pl("c")))  # think=None: untouched
    await env.run_for(5)
    s = env.fakes["cpu"].chat_requests[-1].json
    assert "chat_template_kwargs" not in s and s["max_tokens"] == 77

    env.submit(lspec(4, body=pl("d"), max_tokens=33))  # no task type: job estimate
    await env.run_for(5)
    assert env.fakes["cpu"].chat_requests[-1].json["max_tokens"] == 33


async def test_run_forever_and_close(tmp_path: Path) -> None:
    env = LocalEnv(tmp_path)
    stop = asyncio.Event()
    runner = asyncio.ensure_future(env.worker.run_forever(stop))
    jid = env.submit(lspec(1))
    for _ in range(50):
        await asyncio.sleep(0)
        if env.job(jid)["status"] == "completed":
            break
        env.worker.kick()
    assert env.job(jid)["status"] == "completed"
    # first run probed the backends
    assert env.worker.stats["cpu"].last_probe_at is not None
    stop.set()
    await asyncio.wait_for(runner, 1)


async def test_eta_s(tmp_path: Path) -> None:
    env = LocalEnv(tmp_path)
    now = env.clock.now()
    # own job: 1000/100 + 100/5 = 30 s on the initial EWMA
    assert env.worker.eta_s("m1", 1000, 100, 1, now) == 30.0
    assert env.worker.eta_s("cloudonly", 10, 10, 1, now) is None
    assert env.worker.eta_s("nope", 10, 10, 1, now) is None
    env.submit(lspec(1, est_tokens_in=1000, max_tokens=100))  # queued ahead: +30
    assert env.worker.eta_s("m1", 1000, 100, 1, now) == 60.0
    env.worker.stats["cpu"].busy_external = True
    assert env.worker.eta_s("m1", 1000, 100, 1, now) == 60.0 + 120
    env.worker.stats["cpu"].available = False
    assert env.worker.eta_s("m1", 1000, 100, 1, now) is None
    assert not env.worker.available("cpu") and env.worker.available("cpu2")
