"""T-369 W6: POST /v1/chat/completions (sync wait, SSE, idempotency, disconnect)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, MutableMapping
from pathlib import Path
from typing import Any

import pytest

from api_fixtures import GAME_KEY, PRIV_KEY, ApiEnv, chat_body
from api_gateway_stubs import completion


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[ApiEnv]:
    e = ApiEnv(tmp_path)
    yield e
    await e.aclose()


def sse_events(text: str) -> list[Any]:
    out: list[Any] = []
    for block in text.split("\n\n"):
        if block.startswith("data: "):
            payload = block[6:]
            out.append(payload if payload == "[DONE]" else json.loads(payload))
    return out


async def test_local_answer_200(env: ApiEnv) -> None:
    r = await env.post("/v1/chat/completions", chat_body(model="localonly"), pump_s=2)
    assert r.status_code == 200
    j = r.json()
    assert j["object"] == "chat.completion" and j["model"] == "localonly"
    assert j["choices"][0]["message"]["content"] == "local answer"
    assert j["id"].startswith("chatcmpl-")
    assert r.headers["X-LLMHub-Backend"] == "local:cpu"
    assert r.headers["X-LLMHub-Task"] == j["id"].removeprefix("chatcmpl-")


async def test_cloud_answer_via_mock(env: ApiEnv) -> None:
    r = await env.post("/v1/chat/completions", chat_body(model="cloudonly"), pump_s=420)
    assert r.status_code == 200, r.text
    assert r.headers["X-LLMHub-Backend"] == "cloud:p1"
    assert r.json()["choices"][0]["message"]["content"] == "ok"
    assert float(r.headers["X-LLMHub-Cost-Estimate"]) >= 0


async def test_wait_480_then_503_task_stays_and_retry_joins(env: ApiEnv) -> None:
    env.worker.hold = True
    body = chat_body(model="localonly")
    r = await env.post("/v1/chat/completions", body, pump_s=480)
    assert r.status_code == 503
    err = r.json()["error"]
    assert err["code"] == "queue_wait_exceeded" and err["retryable"] is True
    assert err["task_id"] and int(r.headers["Retry-After"]) >= 5
    job = env.job(err["task_id"])
    assert job["status"] == "queued"
    # repeat of the same request joins the same task and gets the result
    env.worker.hold = False
    task = asyncio.ensure_future(env.post("/v1/chat/completions", body))
    await env.pump(3)
    again = await task
    assert again.status_code == 200
    assert again.headers["X-LLMHub-Task"] == err["task_id"]
    assert len(env.jobs()) == 1


async def test_custom_wait_30(env: ApiEnv) -> None:
    env.worker.hold = True
    r = await env.post(
        "/v1/chat/completions",
        chat_body(model="localonly"),
        headers={"X-LLMHub-Wait": "30"},
        pump_s=31,
    )
    assert r.status_code == 503
    assert env.clock.now() - env.job(r.json()["error"]["task_id"])["created_at"] < 40


async def test_stream_keepalive_then_chunks(env: ApiEnv) -> None:
    env.worker.hold = True
    env.worker.answer = completion(
        "final text", reasoning_content="thinking...",
        tool_calls=[{"id": "call_1", "type": "function",
                     "function": {"name": "f", "arguments": "{\"a\":1}"}}],
    )  # fmt: skip
    task = asyncio.ensure_future(
        env.post(
            "/v1/chat/completions",
            chat_body(model="localonly", stream=True, stream_options={"include_usage": True}),
        )
    )
    await env.pump(40)
    env.worker.hold = False
    await env.pump(3)
    r = await task
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.text.count(": keep-alive") >= 3  # initial + every 15 s
    events = sse_events(r.text)
    assert events[-1] == "[DONE]"
    first = events[0]["choices"][0]["delta"]
    assert first["content"] == "final text" and first["reasoning_content"] == "thinking..."
    call = first["tool_calls"][0]
    assert call["index"] == 0 and call["id"] == "call_1" and call["function"]["name"] == "f"
    assert events[1]["choices"][0]["finish_reason"] == "stop"
    assert events[-2]["usage"]["total_tokens"] == 10 and events[-2]["choices"] == []


async def test_stream_without_usage_option(env: ApiEnv) -> None:
    r = await env.post("/v1/chat/completions", chat_body(model="localonly", stream=True), pump_s=2)
    events = sse_events(r.text)
    assert all("usage" not in e for e in events[:-1])


async def test_stream_timeout_gives_error_event_without_done(env: ApiEnv) -> None:
    env.worker.hold = True
    r = await env.post(
        "/v1/chat/completions",
        chat_body(model="localonly", stream=True),
        headers={"X-LLMHub-Wait": "20"},
        pump_s=25,
    )
    assert r.status_code == 200
    events = sse_events(r.text)
    assert events[-1]["error"]["code"] == "queue_wait_exceeded"
    assert "[DONE]" not in r.text


async def test_stream_failed_job_gives_error_event(env: ApiEnv) -> None:
    env.worker.hold = True
    task = asyncio.ensure_future(
        env.post(
            "/v1/chat/completions",
            chat_body(model="localonly", stream=True),
            headers={"X-LLMHub-Deadline": "10"},
        )
    )
    await env.pump(30)
    r = await task
    events = sse_events(r.text)
    assert events[-1]["error"]["code"] == "deadline_exceeded"


async def test_stream_and_plain_repeat_share_one_task(env: ApiEnv) -> None:
    env.worker.hold = True
    plain = asyncio.ensure_future(env.post("/v1/chat/completions", chat_body(model="localonly")))
    await env.pump(1)
    streamed = asyncio.ensure_future(
        env.post("/v1/chat/completions", chat_body(model="localonly", stream=True))
    )
    await env.pump(1)
    env.worker.hold = False
    await env.pump(2)
    assert (await plain).status_code == 200 and (await streamed).status_code == 200
    assert len(env.jobs()) == 1


async def test_truncated_header(env: ApiEnv) -> None:
    env.worker.answer = completion("cut", finish="length")
    r = await env.post("/v1/chat/completions", chat_body(model="localonly"), pump_s=2)
    assert r.headers["X-LLMHub-Truncated"] == "1"


async def test_idempotency_key_repeat_after_completion(env: ApiEnv) -> None:
    h = {"Idempotency-Key": "k-1"}
    r1 = await env.post("/v1/chat/completions", chat_body(model="localonly"), headers=h, pump_s=2)
    r2 = await env.post("/v1/chat/completions", chat_body(model="localonly"), headers=h)
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json()
    assert len(env.jobs()) == 1
    r3 = await env.post("/v1/chat/completions", chat_body("other", model="localonly"), headers=h)
    assert r3.status_code == 409
    e = r3.json()["error"]
    assert e["code"] == "idempotency_conflict" and e["task_id"] == r1.headers["X-LLMHub-Task"]


async def test_disconnect_does_not_cancel_job(env: ApiEnv) -> None:
    env.worker.hold = True
    sent: list[Any] = []
    body = json.dumps(chat_body(model="localonly")).encode()
    gone = asyncio.Event()
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "path": "/v1/chat/completions", "raw_path": b"/v1/chat/completions", "query_string": b"",
        "headers": [(b"authorization", f"Bearer {GAME_KEY}".encode()),
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode())],
        "client": ("t", 1), "server": ("t", 80), "scheme": "http", "root_path": "",
    }  # fmt: skip
    call = asyncio.ensure_future(env.app(scope, receive, send))
    await env.pump(5)
    assert env.rt.waiter.count == 1
    gone.set()
    await env.pump(2)
    await asyncio.wait_for(call, 5)
    assert env.rt.waiter.count == 0
    (job,) = env.jobs()
    assert job["status"] == "queued" and job["cancel_requested"] == 0
    env.worker.hold = False
    await env.pump(2)
    res = await env.get(f"/v1/tasks/{job['id']}/result")
    assert res.status_code == 200


async def test_validation_errors(env: ApiEnv) -> None:
    r = await env.post("/v1/chat/completions", {"messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"
    r = await env.post("/v1/chat/completions", {"model": "big", "messages": []})
    assert r.status_code == 400
    r = await env.call("POST", "/v1/chat/completions", content="{not json")
    assert r.status_code == 400
    r = await env.call("POST", "/v1/chat/completions", content="[1]")
    assert r.status_code == 400
    r = await env.post("/v1/chat/completions", chat_body(max_tokens=-1))
    assert r.status_code == 400
    r = await env.post("/v1/chat/completions", chat_body(llmhub={"bogus": 1}))
    assert r.status_code == 400


async def test_unknown_and_forbidden_model(env: ApiEnv) -> None:
    r = await env.post("/v1/chat/completions", chat_body(model="nope"))
    assert r.status_code == 404 and r.json()["error"]["code"] == "model_not_available"
    r = await env.post(
        "/v1/chat/completions", chat_body(model="big"), key="lh_test_low_key_0000000000000"
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"


async def test_private_client_goes_local(env: ApiEnv) -> None:
    r = await env.post("/v1/chat/completions", chat_body(model="big"), key=PRIV_KEY, pump_s=2)
    assert r.status_code == 200
    assert r.headers["X-LLMHub-Backend"] == "local:cpu"


async def test_models_list(env: ApiEnv) -> None:
    r = await env.get("/v1/models")
    ids = [m["id"] for m in r.json()["data"]]
    assert ids == ["big", "cloudonly", "localonly", "auto"]
    big = r.json()["data"][0]
    assert big["owned_by"] == "llmhub" and big["llmhub"] == {
        "local": True,
        "cloud": True,
        "ctx_tokens": 65536,
    }
    low = await env.get("/v1/models", key="lh_test_low_key_0000000000000")
    assert [m["id"] for m in low.json()["data"]] == ["localonly"]
