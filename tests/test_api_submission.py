"""T-369 W4: Submitter - canonical body, routing safety, idempotency, leases."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from api_fixtures import PRIV_KEY, ApiEnv, chat_body
from llmhub.api_gateway.submission import canonical_body, estimate_tokens_in

FAKE_AWS = "AKIAABCDEFGHIJKLMNOP"  # fictitious, matches the AWS key id shape


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[ApiEnv]:
    e = ApiEnv(tmp_path)
    yield e
    await e.aclose()


def test_canonical_body_ignores_stream_and_key_order() -> None:
    a = {"model": "m", "messages": [{"role": "user", "content": "x"}], "stream": True,
         "stream_options": {"include_usage": True}, "temperature": 0.1}  # fmt: skip
    b = {"temperature": 0.1, "messages": [{"content": "x", "role": "user"}], "model": "m"}
    assert canonical_body(a) == canonical_body(b)
    assert b" " not in canonical_body(b)
    assert canonical_body({"model": "m", "x": "ключ"}).decode() == '{"model":"m","x":"ключ"}'


def test_estimate_tokens() -> None:
    req = chat_body("x" * 3500)
    assert 1000 <= estimate_tokens_in(req, 3.5) < 1100
    assert estimate_tokens_in(chat_body("x"), 3.5) >= 1


async def test_private_client_is_always_local_even_with_warm_cloud(env: ApiEnv) -> None:
    for i in range(3):
        r = await env.post("/v1/tasks", chat_body(f"q{i}", model="big"), key=PRIV_KEY)
        assert r.status_code == 202
        assert r.json()["route"].startswith("local")
    assert all(j["pool"] is None and j["data_class"] == "private" for j in env.jobs())


async def test_private_cloud_only_alias_is_403(env: ApiEnv) -> None:
    r = await env.post("/v1/chat/completions", chat_body(model="cloudonly"), key=PRIV_KEY)
    assert r.status_code == 403
    assert env.jobs() == []


async def test_header_narrows_cloud_client_to_private(env: ApiEnv) -> None:
    r = await env.post(
        "/v1/tasks", chat_body(model="big"), headers={"X-LLMHub-Data-Class": "private"}
    )
    assert r.status_code == 202 and r.json()["route"].startswith("local")
    assert env.jobs()[0]["data_class"] == "private"


async def test_secret_goes_local_with_warning(env: ApiEnv) -> None:
    r = await env.post("/v1/tasks", chat_body(f"my key {FAKE_AWS}", model="big"))
    assert r.status_code == 202
    (job,) = env.jobs()
    assert job["pool"] is None and "secret_detected" in job["warnings"]
    env.worker.process()
    res = await env.get(f"/v1/tasks/{job['id']}/result")
    assert res.headers["X-LLMHub-Warning"] == "secret_detected"


async def test_secret_in_cloud_only_model_is_403(env: ApiEnv) -> None:
    r = await env.post("/v1/tasks", chat_body(f"k {FAKE_AWS}", model="cloudonly"))
    assert r.status_code == 403
    assert env.jobs() == []


@pytest.mark.parametrize("policy", ["ECONOMY", "BALANCED", "QUALITY"])
async def test_private_or_secret_never_reaches_cloud_when_local_down(
    env: ApiEnv, policy: str
) -> None:
    env.worker.is_available = False
    h = {"X-LLMHub-Policy": policy}
    r1 = await env.post("/v1/tasks", chat_body("hi", model="big"), key=PRIV_KEY, headers=h)
    r2 = await env.post("/v1/tasks", chat_body(f"k {FAKE_AWS}", model="big"), headers=h)
    r3 = await env.post(
        "/v1/tasks", chat_body("hi", model="big"),
        headers={**h, "X-LLMHub-Data-Class": "private"},
    )  # fmt: skip
    for r in (r1, r2, r3):
        assert r.status_code == 503, r.text
        assert r.json()["error"]["code"] == "local_unavailable"
    assert env.jobs("pool IS NOT NULL") == []
    assert env.jobs() == []


async def test_defensive_guard_refuses_cloud_decision_for_private(
    env: ApiEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llmhub.api_gateway import submission
    from llmhub.model_router.rules import RouteDecision

    monkeypatch.setattr(
        submission, "decide",
        lambda *a, **k: RouteDecision("cloud", "p1", None, "bug", None, (), 0.1),
    )  # fmt: skip
    r = await env.post("/v1/tasks", chat_body(model="big"), key=PRIV_KEY)
    assert r.status_code == 403
    assert env.jobs() == []


async def test_llmhub_object_not_sent_to_backend_and_meta_in_body(env: ApiEnv) -> None:
    r = await env.post(
        "/v1/tasks",
        chat_body(model="big", llmhub={"priority": "low", "policy": "ECONOMY"}),
        key=PRIV_KEY,
    )
    assert r.status_code == 202
    (job,) = env.jobs()
    stored = json.loads(env.h.bodies.read_request(job["id"]))
    assert "llmhub" not in stored and "stream" not in stored
    assert stored["_llmhub"] == {"data_class": "private", "policy": "ECONOMY", "priority": "low"}
    assert stored["model"] == "big"
    assert job["priority"] == "low" and job["policy"] == "ECONOMY"


async def test_dedup_differs_by_data_class_policy_priority(env: ApiEnv) -> None:
    body = chat_body("same", model="big")
    ids = set()
    for hdr in ({}, {"X-LLMHub-Data-Class": "private"}, {"X-LLMHub-Policy": "ECONOMY"},
                {"X-LLMHub-Priority": "low"}):  # fmt: skip
        r = await env.post("/v1/tasks", body, headers=hdr)
        ids.add(r.json()["id"])
    assert len(ids) == 4
    again = await env.post("/v1/tasks", body, headers={"X-LLMHub-Priority": "low"})
    assert again.status_code == 200 and again.json()["id"] in ids
    assert len(env.jobs()) == 4


async def test_stream_flag_does_not_change_dedup(env: ApiEnv) -> None:
    a = await env.post("/v1/tasks", chat_body("s", model="big", stream=True))
    b = await env.post("/v1/tasks", chat_body("s", model="big"))
    assert a.json()["id"] == b.json()["id"] and b.status_code == 200


async def test_idempotency_conflict_has_task_id(env: ApiEnv) -> None:
    h = {"Idempotency-Key": "K"}
    a = await env.post("/v1/tasks", chat_body("one", model="big"), headers=h)
    b = await env.post("/v1/tasks", chat_body("two", model="big"), headers=h)
    assert b.status_code == 409
    assert b.json()["error"]["task_id"] == a.json()["id"]


async def test_implicit_dedup_within_window_and_after(env: ApiEnv) -> None:
    body = chat_body("window", model="localonly")
    a = await env.post("/v1/tasks", body)
    env.worker.process()
    await env.pump(600)  # 10 min: inside the ~15 min window
    b = await env.post("/v1/tasks", body)
    assert b.json()["id"] == a.json()["id"]
    await env.pump(600)  # past the window
    c = await env.post("/v1/tasks", body)
    assert c.json()["id"] != a.json()["id"]


async def test_lease_header_checks(env: ApiEnv) -> None:
    body = chat_body(model="big")
    r = await env.post("/v1/tasks", body, headers={"X-LLMHub-Lease": "nope"})
    assert r.status_code == 404
    lease = (await env.post("/v1/leases", {"model": "big", "ttl_s": 60})).json()
    ok = await env.post("/v1/tasks", body, headers={"X-LLMHub-Lease": lease["id"]})
    assert ok.status_code == 202
    other = await env.post(
        "/v1/tasks", chat_body("o", model="localonly"), key="lh_test_low_key_0000000000000",
        headers={"X-LLMHub-Lease": lease["id"]},
    )  # fmt: skip
    assert other.status_code == 404  # somebody else's lease
    await env.pump(61)
    expired = await env.post(
        "/v1/tasks", chat_body("e", model="big"), headers={"X-LLMHub-Lease": lease["id"]}
    )
    assert expired.status_code == 410
    await env.post(f"/v1/leases/{lease['id']}/close")
    closed_lease = (await env.post("/v1/leases", {"model": "big", "ttl_s": 60})).json()
    await env.post(f"/v1/leases/{closed_lease['id']}/close")
    closed = await env.post(
        "/v1/tasks", chat_body("c", model="big"), headers={"X-LLMHub-Lease": closed_lease["id"]}
    )
    assert closed.status_code == 409 and closed.json()["error"]["code"] == "lease_closed"


async def test_economy_stage_forces_economy_for_normal(env: ApiEnv) -> None:
    now = env.clock.now()
    env.h.conn.execute(
        "INSERT INTO ledger(provider, pool, kind, from_ts, to_ts, amount_usd, source, created_at)"
        " VALUES ('mock','p1','gpu',?,?,?, 'estimate', ?)",
        (now - 100, now - 50, env.cfg.budget.budget_month * 0.85, now),
    )
    r = await env.post("/v1/tasks", chat_body("eco", model="big"))
    assert r.status_code == 202
    (job,) = env.jobs()
    assert job["policy"] == "ECONOMY" and "economy_stage" in (job["warnings"] or "")


async def test_context_too_large(env: ApiEnv) -> None:
    big = "x" * (4 * 32768 * 4)
    r = await env.post("/v1/tasks", chat_body(big, model="localonly"))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "context_too_large"


SIX = [
    ("insufficient_balance", False), ("quota_exceeded", False), ("provider_api_error", True),
    ("gpu_unavailable", True), ("deployment_error", True), ("budget_limit_reached", False),
]  # fmt: skip


class _DownCloud:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    def view(self, alias: str, tokens_in: int, max_tokens: int, priority: Any) -> Any:
        from llmhub.model_router.rules import CloudView

        return CloudView("p1", 65536, False, 600.0, self.reason, 0.1, 30.0)  # type: ignore[arg-type]


@pytest.mark.parametrize(("reason", "retryable"), SIX)
async def test_six_unavailability_reasons_at_the_api(
    env: ApiEnv, reason: str, retryable: bool
) -> None:
    env.rt.cloud = _DownCloud(reason)
    # (a) alias with a local model: served locally, the reason is reported
    ok = await env.post("/v1/chat/completions", chat_body(model="big"), pump_s=2)
    assert ok.status_code == 200
    assert (
        ok.headers["X-LLMHub-Fallback"] == reason and ok.headers["X-LLMHub-Backend"] == "local:cpu"
    )
    # (b) cloud-only alias: 503 with the reason and the retryable flag of the table
    r = await env.post("/v1/chat/completions", chat_body(model="cloudonly"))
    assert r.status_code == 503
    e = r.json()["error"]
    assert e["code"] == "cloud_unavailable" and e["reason"] == reason
    assert e["retryable"] is retryable
    # (c) private client, local down: 503 and nothing is queued for the cloud
    env.worker.is_available = False
    p = await env.post("/v1/tasks", chat_body("p", model="big"), key=PRIV_KEY)
    assert p.status_code == 503
    assert env.jobs("pool IS NOT NULL") == []
