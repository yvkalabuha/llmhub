"""T-369 W7: /v1/tasks."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from api_fixtures import LOW_KEY, PRIV_KEY, ApiEnv, chat_body


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[ApiEnv]:
    e = ApiEnv(tmp_path)
    yield e
    await e.aclose()


async def test_submit_202_location_and_duplicate_200(env: ApiEnv) -> None:
    env.worker.hold = True
    r = await env.post("/v1/tasks", chat_body(model="localonly"))
    assert r.status_code == 202
    j = r.json()
    assert j["object"] == "llmhub.task" and j["status"] == "queued" and j["route"] == "local"
    assert r.headers["Location"] == f"/v1/tasks/{j['id']}"
    dup = await env.post("/v1/tasks", chat_body(model="localonly"))
    assert dup.status_code == 200 and dup.json()["id"] == j["id"]


async def test_status_has_eta_route_and_fields(env: ApiEnv) -> None:
    env.worker.hold = True
    jid = (await env.post("/v1/tasks", chat_body(model="localonly"))).json()["id"]
    s = (await env.get(f"/v1/tasks/{jid}")).json()
    assert s["status"] == "queued" and s["route"] == "local" and s["eta_s"] == 10.0
    assert s["priority"] == "normal" and s["policy"] == "BALANCED" and s["error"] is None
    assert s["warnings"] == [] and s["usage"] is None and s["pool"] is None
    env.worker.hold = False
    await env.pump(2)
    s = (await env.get(f"/v1/tasks/{jid}")).json()
    assert s["status"] == "completed" and s["eta_s"] is None
    assert s["usage"] == {"prompt_tokens": 7, "completion_tokens": 3}
    assert s["backend"] == "local:cpu" and s["cost_usd"] == 0.0


async def test_cloud_status_eta(env: ApiEnv) -> None:
    jid = (await env.post("/v1/tasks", chat_body(model="cloudonly"))).json()["id"]
    s = (await env.get(f"/v1/tasks/{jid}")).json()
    assert s["pool"] == "p1" and s["backend"] == "cloud:p1" and s["eta_s"] > 0


async def test_result_not_ready_then_ok(env: ApiEnv) -> None:
    env.worker.hold = True
    jid = (await env.post("/v1/tasks", chat_body(model="localonly"))).json()["id"]
    r = await env.get(f"/v1/tasks/{jid}/result")
    assert r.status_code == 409
    e = r.json()["error"]
    assert e["code"] == "task_not_ready" and e["retryable"] and e["retry_after_s"] >= 5
    assert e["task_id"] == jid and "Retry-After" in r.headers
    env.worker.hold = False
    await env.pump(2)
    ok = await env.get(f"/v1/tasks/{jid}/result")
    assert ok.status_code == 200
    assert ok.json()["choices"][0]["message"]["content"] == "local answer"
    assert ok.headers["X-LLMHub-Task"] == jid


async def test_deadline_expired_is_failed_and_504(env: ApiEnv) -> None:
    env.worker.hold = True
    jid = (
        await env.post("/v1/tasks", chat_body(model="localonly", llmhub={"deadline_s": 20}))
    ).json()["id"]
    await env.pump(25)
    s = (await env.get(f"/v1/tasks/{jid}")).json()
    assert s["status"] == "failed" and s["error"]["code"] == "deadline_exceeded"
    r = await env.get(f"/v1/tasks/{jid}/result")
    assert r.status_code == 504 and r.json()["error"]["code"] == "deadline_exceeded"


async def test_cancel_queued(env: ApiEnv) -> None:
    env.worker.hold = True
    jid = (await env.post("/v1/tasks", chat_body(model="localonly"))).json()["id"]
    c = await env.post(f"/v1/tasks/{jid}/cancel")
    assert c.status_code == 200 and c.json()["status"] == "cancelled"
    r = await env.get(f"/v1/tasks/{jid}/result")
    assert r.status_code == 409 and r.json()["error"]["code"] == "cancelled"
    again = await env.post(f"/v1/tasks/{jid}/cancel")  # terminal: unchanged
    assert again.status_code == 200 and again.json()["status"] == "cancelled"


async def test_cancel_running_local_breaks_upstream(env: ApiEnv) -> None:
    env.worker.hang = True
    jid = (await env.post("/v1/tasks", chat_body(model="localonly"))).json()["id"]
    await env.pump(2)
    assert env.job(jid)["status"] == "running"
    c = await env.post(f"/v1/tasks/{jid}/cancel")
    assert c.status_code == 200
    assert env.worker.cancelled == [jid]
    await env.pump(1)
    assert env.job(jid)["status"] == "cancelled"


async def test_cancel_running_cloud_ends_cancelled(env: ApiEnv) -> None:
    jid = (await env.post("/v1/tasks", chat_body(model="cloudonly"))).json()["id"]
    for _ in range(100):
        await env.pump(5)
        if env.job(jid)["status"] == "running":
            break
    assert env.job(jid)["status"] == "running"
    c = await env.post(f"/v1/tasks/{jid}/cancel")
    assert c.json()["status"] == "running" and c.json()["cancel_requested"] is True
    await env.pump(120)
    assert env.job(jid)["status"] == "cancelled"
    r = await env.get(f"/v1/tasks/{jid}/result")
    assert r.status_code == 409 and r.json()["error"]["code"] == "cancelled"


async def test_foreign_task_is_404_everywhere(env: ApiEnv) -> None:
    jid = (await env.post("/v1/tasks", chat_body(model="localonly"))).json()["id"]
    for method, path in (("GET", ""), ("GET", "/result"), ("POST", "/cancel")):
        r = await env.call(method, f"/v1/tasks/{jid}{path}", key=PRIV_KEY)
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert (await env.get("/v1/tasks/doesnotexist")).status_code == 404
    assert env.job(jid)["status"] != "cancelled"


async def test_llmhub_field_beats_header(env: ApiEnv) -> None:
    r = await env.post(
        "/v1/tasks", chat_body(model="localonly", llmhub={"priority": "low", "policy": "ECONOMY"}),
        headers={"X-LLMHub-Priority": "normal", "X-LLMHub-Policy": "QUALITY"},
    )  # fmt: skip
    job = env.job(r.json()["id"])
    assert job["priority"] == "low" and job["policy"] == "ECONOMY"


async def test_priority_over_max_is_403_and_bad_llmhub_400(env: ApiEnv) -> None:
    r = await env.post("/v1/tasks", chat_body(model="localonly", llmhub={"priority": "urgent"}))
    assert r.status_code == 403 and r.json()["error"]["reason"] == "priority_not_allowed"
    r = await env.post("/v1/tasks", chat_body(model="localonly", llmhub=[1]))
    assert r.status_code == 400
    r = await env.post("/v1/tasks", chat_body(model="localonly", llmhub={"task_type": "zzz"}))
    assert r.status_code == 400
    r = await env.post("/v1/tasks", chat_body(model="localonly"), key=LOW_KEY)
    assert r.status_code == 202 and env.job(r.json()["id"])["priority"] == "low"


async def test_cloud_unavailable_decided_before_posting(env: ApiEnv) -> None:
    env.h.conn.execute(
        "INSERT INTO provider_accounts(provider, blocked_reason) VALUES ('mock','quota_exceeded')"
        " ON CONFLICT(provider) DO UPDATE SET blocked_reason=excluded.blocked_reason"
    )
    env.cloud.invalidate()
    r = await env.post("/v1/tasks", chat_body(model="cloudonly"))
    assert r.status_code == 503
    e = r.json()["error"]
    assert e["code"] == "cloud_unavailable" and e["reason"] == "quota_exceeded"
    assert e["retryable"] is False
    assert env.jobs() == []
