"""T-369 I3: end to end through the real composition root on a FakeClock.

Batching (10 parallel requests -> 1 create), private data stays local even with a warm GPU,
budget ECONOMY stage, process restart in the middle of a local and a cloud job, and an
idempotent repeat after the restart.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from api_fixtures import PRIV_KEY, chat_body
from int_fixtures import IntEnv, settle

TERMINAL = ("completed", "failed", "cancelled")


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[IntEnv]:
    e = await IntEnv(tmp_path).start()
    yield e
    await e.stop()


async def test_ten_parallel_cloud_requests_one_create(env: IntEnv) -> None:
    calls = [
        asyncio.ensure_future(
            env.client.post(
                "/v1/chat/completions", json=chat_body(f"q{i}", model="cloudonly", max_tokens=200),
                headers={"Authorization": f"Bearer {env_key()}"},
            )
        )
        for i in range(10)
    ]  # fmt: skip
    await settle()
    while not all(c.done() for c in calls):
        await env.pump(5)
    rs = [c.result() for c in calls]
    assert [r.status_code for r in rs] == [200] * 10
    assert {r.headers["X-LLMHub-Backend"] for r in rs} == {"cloud:p1"}
    assert env.provider.create_calls == 1
    assert len({r.json()["id"] for r in rs}) == 10

    # warm GPU: a cloud-allowed BALANCED request goes to it, a private one stays local
    warm = await env.post("/v1/chat/completions", chat_body("w", model="big"), pump_s=60)
    assert warm.status_code == 200 and warm.headers["X-LLMHub-Backend"] == "cloud:p1"
    for i in range(3):
        p = await env.post("/v1/chat/completions", chat_body(f"p{i}", model="big"),
                           key=PRIV_KEY, pump_s=60)  # fmt: skip
        assert p.status_code == 200 and p.headers["X-LLMHub-Backend"] == "local:cpu"
        assert "X-LLMHub-Fallback" not in p.headers
    assert env.jobs("client_id='priv' AND pool IS NOT NULL") == []


def env_key() -> str:
    from api_fixtures import GAME_KEY

    return GAME_KEY


async def test_economy_stage_at_80_percent(env: IntEnv) -> None:
    now = env.clock.now()
    env.conn.execute(
        "INSERT INTO ledger(provider, pool, kind, from_ts, to_ts, amount_usd, source, created_at)"
        " VALUES ('mock','p1','gpu',?,?,?, 'estimate', ?)",
        (now - 100, now - 50, env.cfg.budget.budget_month * 0.85, now),
    )
    env.rt.cloud.invalidate()  # type: ignore[attr-defined]
    r = await env.post("/v1/chat/completions", chat_body("eco", model="big"), pump_s=60)
    assert r.status_code == 200
    assert "economy_stage" in r.headers.get("X-LLMHub-Warning", "")
    (job,) = env.jobs()
    assert job["policy"] == "ECONOMY"


async def test_restart_mid_local_and_cloud_job(env: IntEnv) -> None:
    env.fakes["cpu"].delays.append(120.0)  # the first local call hangs over the crash
    lo = await env.post("/v1/tasks", chat_body("loc", model="localonly"),
                        headers={"Idempotency-Key": "loc-1"})  # fmt: skip
    cl = await env.post("/v1/tasks", chat_body("cld", model="cloudonly", max_tokens=4000),
                        headers={"Idempotency-Key": "cld-1"})  # fmt: skip
    assert lo.status_code == cl.status_code == 202
    lid, cid = lo.json()["id"], cl.json()["id"]
    await env.pump_until(
        lambda: env.job(lid)["status"] == "running" and env.job(cid)["status"] == "running",
        limit_s=600,
    )
    old_boot = env.rt.controller.boot_id
    await env.restart()
    assert env.rt.controller.boot_id != old_boot
    assert env.rt.worker.boot_id == env.rt.controller.boot_id  # type: ignore[attr-defined]
    await env.pump_until(
        lambda: env.job(lid)["status"] in TERMINAL and env.job(cid)["status"] in TERMINAL,
        limit_s=1200,
    )
    for tid, backend in ((lid, "local:cpu"), (cid, "cloud:p1")):
        res = await env.get(f"/v1/tasks/{tid}/result")
        assert res.status_code == 200, res.text
        assert res.headers["X-LLMHub-Backend"] == backend
        assert env.job(tid)["restarts"] >= 1
    assert env.fakes["cpu"].aborted >= 1  # the hanging call died with the old process
    # idempotent repeat after the restart: same task, no new job
    n = len(env.jobs())
    again = await env.post("/v1/tasks", chat_body("loc", model="localonly"),
                           headers={"Idempotency-Key": "loc-1"})  # fmt: skip
    assert again.status_code == 200 and again.json()["id"] == lid
    sync = await env.post("/v1/chat/completions", chat_body("cld", model="cloudonly",
                          max_tokens=4000), headers={"Idempotency-Key": "cld-1"})  # fmt: skip
    assert sync.status_code == 200 and sync.headers["X-LLMHub-Task"] == cid
    assert len(env.jobs()) == n


async def test_graceful_stop_keeps_rows_for_next_boot(env: IntEnv) -> None:
    env.fakes["cpu"].delays.append(300.0)
    r = await env.post("/v1/tasks", chat_body("g", model="localonly"))
    tid = r.json()["id"]
    await env.pump_until(lambda: env.job(tid)["status"] == "running", limit_s=60)
    await env.stop()  # lifespan exit: loops stop, the worker leaves the row as is
    await env.start()
    await env.pump_until(lambda: env.job(tid)["status"] == "completed", limit_s=600)
    res = await env.get(f"/v1/tasks/{tid}/result")
    assert res.status_code == 200
