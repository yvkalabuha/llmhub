"""T-369 I2: the six cloud-unavailability reasons from real sources, through the real stack.

Sources: Mock faults (balance, quota, 5xx -> breaker), `gpu_available=False`, failing model
tests (three failed cycles), budget reservation. For each reason:
(a) alias with a local model -> 200 from `local:cpu` with `X-LLMHub-Fallback: <reason>`;
(b) cloud-only alias -> 503 `cloud_unavailable`, `reason`, `retryable` per the error table
    (refused before queueing when the reason is known, or failed after the core's fallback);
(c) private client with the local backend down -> 503, nothing is ever queued for the cloud.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from api_fixtures import PRIV_KEY, chat_body
from int_fixtures import IntEnv
from llmhub.budget_manager import reservations
from llmhub.provider_adapters.mock import Fault, MockKnobs
from llmhub.provider_adapters.mock_inference import MockInferenceKnobs

SIX = {
    "insufficient_balance": False, "quota_exceeded": False, "provider_api_error": True,
    "gpu_unavailable": True, "deployment_error": True, "budget_limit_reached": False,
}  # fmt: skip
QUALITY = {"X-LLMHub-Policy": "QUALITY"}
TERMINAL = ("completed", "failed", "cancelled")


@pytest.fixture(params=list(SIX))
async def down(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[tuple[IntEnv, str]]:
    reason: str = request.param
    env = IntEnv(
        tmp_path,
        knobs=MockKnobs(gpu_available=reason != "gpu_unavailable", offers=12),  # fresh hosts
        inference_knobs=MockInferenceKnobs(test_fail=1000 if reason == "deployment_error" else 0),
    )
    await env.start()
    if reason == "insufficient_balance":
        env.provider.inject("create", Fault.INSUFFICIENT_BALANCE)
    elif reason == "quota_exceeded":
        env.provider.inject("create", Fault.QUOTA)
    elif reason == "provider_api_error":
        env.provider.inject("any", Fault.API_5XX, times=10_000)
    elif reason == "budget_limit_reached":
        reservations.reserve(env.conn, "session", "external", 45.0, env.clock.now())
    yield env, reason
    await env.stop()


async def _task(env: IntEnv, body: dict[str, Any], **kw: Any) -> tuple[int, dict[str, Any]]:
    r = await env.post("/v1/tasks", body, **kw)
    return r.status_code, r.json()


async def _finish(env: IntEnv, task_id: str, limit_s: float = 5000) -> Any:
    await env.pump_until(lambda: env.job(task_id)["status"] in TERMINAL, limit_s, slice_s=20)
    return await env.get(f"/v1/tasks/{task_id}/result")


async def test_six_reasons_three_ways(down: tuple[IntEnv, str]) -> None:
    env, reason = down
    # trip the reason with a cloud-preferring request (QUALITY), served locally in the end
    code, task = await _task(env, chat_body("first", model="big"), headers=QUALITY)
    assert code == 202
    first = await _finish(env, task["id"])
    assert first.status_code == 200, first.text
    assert first.headers["X-LLMHub-Backend"] == "local:cpu"
    assert env.job(task["id"])["pool"] is None or env.job(task["id"])["route"] == "local:fallback"
    if reason != "provider_api_error":
        # (the core reports a failing offer search as gpu_unavailable; the breaker opened
        # by those 5xx is what the API reports from now on: see the second request)
        assert first.headers["X-LLMHub-Fallback"] == reason

    # (a) the next request with a local model: 200 locally with the reason
    code, again = await _task(env, chat_body("second", model="big"), headers=QUALITY)
    assert code == 202
    res = await _finish(env, again["id"])
    assert res.status_code == 200, res.text
    assert res.headers["X-LLMHub-Backend"] == "local:cpu"
    assert res.headers["X-LLMHub-Fallback"] == reason

    # (b) cloud-only alias: 503 cloud_unavailable with the reason and the table's retryable
    code, body = await _task(env, chat_body("third", model="cloudonly"))
    if code == 202:  # reason not visible before queueing: the core falls back, the job fails
        r = await _finish(env, body["id"])
        status, err = r.status_code, r.json()["error"]
    else:
        status, err = code, body["error"]
    assert status == 503
    assert err["code"] == "cloud_unavailable" and err["reason"] == reason
    assert err["retryable"] is SIX[reason]

    # (c) private client, local backend down: 503, never a cloud job for it
    for fake in env.fakes.values():
        fake.health_status = 503
        fake.slots_status = 503
    await env.pump(4 * env.cfg.local.probe_interval_s)
    assert not env.rt.worker.available("cpu")
    code, body = await _task(env, chat_body("private", model="big"), key=PRIV_KEY)
    assert code == 503 and body["error"]["code"] == "local_unavailable"
    assert env.jobs("client_id='priv' AND pool IS NOT NULL") == []
    assert env.jobs("client_id='priv'") == []


async def test_fallback_header_only_for_cloud_eligible_requests(tmp_path: Path) -> None:
    """Private data never had the cloud as an option: no X-LLMHub-Fallback for it."""
    env = await IntEnv(tmp_path).start()
    try:
        reservations.reserve(env.conn, "session", "external", 45.0, env.clock.now())
        env.rt.cloud.invalidate()  # type: ignore[attr-defined]
        game = await env.post("/v1/chat/completions", chat_body("g", model="big"), pump_s=30)
        assert game.headers["X-LLMHub-Fallback"] == "budget_limit_reached"
        priv = await env.post("/v1/chat/completions", chat_body("p", model="big"), key=PRIV_KEY,
                              pump_s=30)  # fmt: skip
        assert priv.status_code == 200 and priv.headers["X-LLMHub-Backend"] == "local:cpu"
        assert "X-LLMHub-Fallback" not in priv.headers
        secret = chat_body("my key: api_key=sk-test-0000000000000000000000", model="big")
        sec = await env.post("/v1/chat/completions", secret, pump_s=30)
        assert sec.status_code == 200 and "X-LLMHub-Fallback" not in sec.headers
        assert "secret_detected" in sec.headers["X-LLMHub-Warning"]
    finally:
        await env.stop()
