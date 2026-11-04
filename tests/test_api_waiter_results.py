"""T-369 W5: JobWaiter (one poller) and result conversion."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from api_fixtures import ApiEnv
from harness import Harness, make_job
from llmhub.api_gateway.errors import ApiError, ErrorCode
from llmhub.api_gateway.results import (
    job_error,
    response_headers,
    to_chat_completion,
    to_sse_chunks,
)
from llmhub.api_gateway.waiter import JobWaiter
from llmhub.task_queue import local
from llmhub.task_queue.models import JobSpec


@pytest.fixture
async def hw(tmp_path: Path):  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    w = JobWaiter(h.conn, h.clock, 0.5)
    stop = asyncio.Event()
    poller = asyncio.ensure_future(w.run(stop))
    yield h, w
    stop.set()
    poller.cancel()
    await asyncio.gather(poller, return_exceptions=True)


def local_job(n: int) -> JobSpec:
    return replace(make_job(n), pool=None, route="local")


def submit_local(h: Harness, n: int = 0) -> str:
    spec = local_job(n)
    return h.submit(spec).job_id


def finish(h: Harness, job_id: str) -> None:
    now = h.clock.now()
    local.mark_running_local(h.conn, job_id, "w", "local:cpu", now)
    from llmhub.cloud_manager.inference import JobRunResult

    local.complete_local(h.conn, job_id, "w", JobRunResult(1, 1, "stop", b"x"), now, "")


async def test_wait_returns_on_notify_without_advancing_clock(hw) -> None:  # type: ignore[no-untyped-def]
    h, w = hw
    jid = submit_local(h)
    t0 = h.clock.now()
    task = asyncio.ensure_future(w.wait(jid, t0 + 100))
    await h._settle()
    assert not task.done()
    finish(h, jid)
    w.notify(jid)
    await h._settle()
    row = await asyncio.wait_for(task, 1)
    assert row["status"] == "completed" and h.clock.now() == t0


async def test_wait_returns_on_poll(hw) -> None:  # type: ignore[no-untyped-def]
    h, w = hw
    jid = submit_local(h)
    task = asyncio.ensure_future(w.wait(jid, h.clock.now() + 100))
    await h._settle()
    finish(h, jid)  # no notify
    await h.drive(asyncio.sleep(0), limit=None)
    h.clock.advance_to(h.clock.now() + 0.5)
    await h._settle()
    assert (await asyncio.wait_for(task, 1))["status"] == "completed"


async def test_wait_until_returns_current_row(hw) -> None:  # type: ignore[no-untyped-def]
    h, w = hw
    jid = submit_local(h)
    task = asyncio.ensure_future(w.wait(jid, h.clock.now() + 10))
    await h._settle()
    h.clock.advance_to(h.clock.now() + 10)
    await h._settle()
    row = await asyncio.wait_for(task, 1)
    assert row["status"] == "queued"


async def test_wait_already_terminal_and_missing(hw) -> None:  # type: ignore[no-untyped-def]
    h, w = hw
    jid = submit_local(h)
    finish(h, jid)
    assert (await w.wait(jid, h.clock.now() + 5))["status"] == "completed"
    assert await w.wait("nope", h.clock.now() + 5) is None


async def test_disconnect_returns_none_and_job_not_cancelled(hw) -> None:  # type: ignore[no-untyped-def]
    h, w = hw
    jid = submit_local(h)
    gone = False

    async def disconnected() -> bool:
        return gone

    task = asyncio.ensure_future(w.wait(jid, h.clock.now() + 1000, disconnected))
    await h._settle()
    gone = True
    h.clock.advance_to(h.clock.now() + 0.5)
    await h._settle()
    assert await asyncio.wait_for(task, 1) is None
    row = h.conn.execute("SELECT status, cancel_requested FROM jobs WHERE id=?", (jid,)).fetchone()
    assert (row["status"], row["cancel_requested"]) == ("queued", 0)
    assert w.count == 0


async def test_one_select_per_tick_for_100_waiters(hw) -> None:  # type: ignore[no-untyped-def]
    h, w = hw
    ids = [submit_local(h, i) for i in range(100)]
    tasks = [asyncio.ensure_future(w.wait(i, h.clock.now() + 1000)) for i in ids]
    await h._settle()
    assert w.count == 100
    statements: list[str] = []
    h.conn.set_trace_callback(statements.append)
    h.clock.advance_to(h.clock.now() + 0.5)
    await h._settle()
    h.conn.set_trace_callback(None)
    selects = [s for s in statements if "FROM jobs" in s]
    assert len(selects) == 1 and "IN (" in selects[0]
    for i in ids:
        finish(h, i)
    h.clock.advance_to(h.clock.now() + 0.5)
    await h._settle()
    rows = await asyncio.wait_for(asyncio.gather(*tasks), 2)
    assert all(r["status"] == "completed" for r in rows)
    assert w.count == 0


async def test_cancelled_waiter_is_removed(hw) -> None:  # type: ignore[no-untyped-def]
    h, w = hw
    jid = submit_local(h)
    task = asyncio.ensure_future(w.wait(jid, h.clock.now() + 1000))
    await h._settle()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert w.count == 0


# --- results ---------------------------------------------------------------


@pytest.fixture
def env_rows(tmp_path: Path):  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)

    def make(**fields: object) -> sqlite3.Row:
        jid = h.submit(local_job(len(fields) + 1)).job_id
        for k, v in fields.items():
            h.conn.execute(f"UPDATE jobs SET {k}=? WHERE id=?", (v, jid))
        row: sqlite3.Row = h.conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        return row

    return make


def test_to_chat_completion_replaces_id_and_model(env_rows) -> None:  # type: ignore[no-untyped-def]
    job = env_rows(status="completed", finished_at=1234.5)
    body = json.dumps({"id": "x", "model": "gguf-file", "object": "chat.completion",
                       "choices": [{"index": 0, "message": {"role": "assistant", "content": "a"},
                                    "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 1, "completion_tokens": 2,
                                 "total_tokens": 3}}).encode()  # fmt: skip
    out = to_chat_completion(job, body)
    assert out["id"] == f"chatcmpl-{job['id']}" and out["model"] == "m1"
    assert out["usage"]["total_tokens"] == 3 and out["created"] == 1234


def test_to_chat_completion_wraps_non_json(env_rows) -> None:  # type: ignore[no-untyped-def]
    job = env_rows(status="completed", tokens_in=4, tokens_out=6, finish_reason="stop")
    out = to_chat_completion(job, b"ok")
    assert out["choices"][0]["message"]["content"] == "ok"
    assert out["usage"] == {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10}
    # JSON that is not a chat.completion is wrapped as text too
    assert to_chat_completion(job, b'{"a":1}')["choices"][0]["message"]["content"] == '{"a":1}'


def test_sse_chunks_shape() -> None:
    completion = {
        "id": "chatcmpl-1", "created": 5, "model": "m",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": "hi", "reasoning_content": "hmm",
            "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "f", "arguments": "{}"}},
                {"id": "b", "function": {"name": "g", "arguments": "{\"x\":1}"}},
            ]}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }  # fmt: skip
    chunks = to_sse_chunks(completion, include_usage=True)
    assert all(c.startswith("data: ") and c.endswith("\n\n") for c in chunks)
    assert chunks[-1] == "data: [DONE]\n\n"
    ev = [json.loads(c[6:]) for c in chunks[:-1]]
    d = ev[0]["choices"][0]["delta"]
    assert d["role"] == "assistant" and d["content"] == "hi" and d["reasoning_content"] == "hmm"
    assert [t["index"] for t in d["tool_calls"]] == [0, 1]
    assert d["tool_calls"][1]["type"] == "function"
    assert ev[0]["object"] == "chat.completion.chunk"
    assert ev[1]["choices"][0]["finish_reason"] == "tool_calls"
    assert ev[2]["usage"]["total_tokens"] == 2 and ev[2]["choices"] == []
    assert len(to_sse_chunks(completion, include_usage=False)) == len(chunks) - 1


def test_response_headers(env_rows) -> None:  # type: ignore[no-untyped-def]
    job = env_rows(
        status="completed", backend="local:cpu", fallback_reason="gpu_unavailable",
        finish_reason="length", est_cost_usd=0.5, warnings="secret_detected,economy_stage",
    )  # fmt: skip
    h = response_headers(job)
    assert h["X-LLMHub-Task"] == job["id"] and h["X-LLMHub-Backend"] == "local:cpu"
    assert h["X-LLMHub-Fallback"] == "gpu_unavailable" and h["X-LLMHub-Truncated"] == "1"
    assert h["X-LLMHub-Cost-Estimate"] == "0.500000"
    assert h["X-LLMHub-Warning"] == "secret_detected,economy_stage"
    plain = response_headers(env_rows(status="completed", finish_reason="stop"))
    assert (
        set(plain) == {"X-LLMHub-Task", "X-LLMHub-Cost-Estimate"}
        or "X-LLMHub-Truncated" not in plain
    )


@pytest.mark.parametrize(
    ("status", "code", "reason", "expect"),
    [
        ("cancelled", None, None, ErrorCode.CANCELLED),
        ("failed", "deadline_exceeded", None, ErrorCode.DEADLINE_EXCEEDED),
        ("failed", "queue_wait_exceeded", None, ErrorCode.QUEUE_WAIT_EXCEEDED),
        ("failed", "cloud_unavailable", "insufficient_balance", ErrorCode.CLOUD_UNAVAILABLE),
        ("failed", "local_unavailable", None, ErrorCode.LOCAL_UNAVAILABLE),
        ("failed", "upstream_timeout", None, ErrorCode.UPSTREAM_TIMEOUT),
        ("failed", "context_too_large", None, ErrorCode.CONTEXT_TOO_LARGE),
        ("failed", "weird_code", None, ErrorCode.INTERNAL_ERROR),
        ("failed", None, None, ErrorCode.INTERNAL_ERROR),
    ],
)
def test_job_error_mapping(env_rows, status, code, reason, expect) -> None:  # type: ignore[no-untyped-def]
    job = env_rows(status=status, error_code=code, fallback_reason=reason)
    err = job_error(job)
    assert isinstance(err, ApiError) and err.code is expect and err.task_id == job["id"]
    if reason:
        assert err.reason == reason
    assert not err.retryable if expect is ErrorCode.CLOUD_UNAVAILABLE else True


async def test_waiters_gauge_via_env(tmp_path: Path) -> None:
    e = ApiEnv(tmp_path)
    try:
        e.worker.hold = True
        task = asyncio.ensure_future(
            e.post(
                "/v1/chat/completions",
                {"model": "localonly", "messages": [{"role": "user", "content": "x"}]},
            )
        )
        await e.pump(2)
        assert e.waiter.count == 1
        e.worker.hold = False
        await e.pump(2)
        assert (await task).status_code == 200
        assert e.waiter.count == 0
    finally:
        await e.aclose()
