"""Review T-369 cycle 2 (correctness/reliability): client retry semantics, worker loop survival.

Defects are `xfail(strict=True)`: remove the marker once fixed. Passing tests pin the
premises the findings rely on (openai-python retry rules, dedup scope).
"""

from __future__ import annotations

import asyncio
import types
from collections.abc import AsyncIterator
from pathlib import Path

import openai
import pytest

from api_fixtures import ApiEnv, chat_body
from h_fixtures import LocalEnv
from llmhub.persistence.db import Busy
from llmhub.task_queue import local


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[ApiEnv]:
    e = ApiEnv(tmp_path)
    yield e
    await e.aclose()


def _openai_would_retry(status: int, headers: dict[str, str]) -> bool:
    client = openai.OpenAI(api_key="x", base_url="http://unused.invalid/v1")
    resp = types.SimpleNamespace(status_code=status, headers=headers)
    return bool(client._should_retry(resp))  # pyright: ignore[reportPrivateUsage, reportArgumentType]


# --- FN-1: openai-python auto-retries 409/5xx; non-retryable errors must say so ---------------


def test_premise_openai_retries_409_and_5xx_without_hint() -> None:
    assert _openai_would_retry(409, {})
    assert _openai_would_retry(503, {})
    assert _openai_would_retry(504, {})
    assert not _openai_would_retry(409, {"x-should-retry": "false"})


async def test_premise_resubmit_after_cancel_creates_new_job(env: ApiEnv) -> None:
    """What an openai-python retry after `409 cancelled` does today: the same body is not
    deduplicated against a cancelled job, so the cancelled work is started again."""
    env.worker.hold = True
    body = chat_body(model="localonly")
    jid = (await env.post("/v1/tasks", body)).json()["id"]
    await env.post(f"/v1/tasks/{jid}/cancel")
    again = (await env.post("/v1/tasks", body)).json()["id"]
    assert again != jid and env.job(again)["status"] == "queued"


async def test_non_retryable_error_tells_sdk_not_to_retry(env: ApiEnv) -> None:
    env.worker.hold = True
    jid = (await env.post("/v1/tasks", chat_body(model="localonly"))).json()["id"]
    await env.post(f"/v1/tasks/{jid}/cancel")
    r = await env.get(f"/v1/tasks/{jid}/result")
    assert r.status_code == 409 and r.json()["error"]["retryable"] is False
    assert not _openai_would_retry(r.status_code, dict(r.headers))


# --- FN-2: explicit Idempotency-Key pins a retryable failure for 7 days ------------------------


async def test_retry_with_same_key_after_retryable_failure_runs_again(env: ApiEnv) -> None:
    """integration.md §6: 'retry only retryable=true ... with the same Idempotency-Key'.
    Today the key returns the failed job again, so the advised retry can never succeed."""
    env.worker.hold = True
    h = {"Idempotency-Key": "k-retry"}
    body = chat_body(model="localonly")
    jid = (await env.post("/v1/tasks", body, headers=h)).json()["id"]
    local.fail(env.h.conn, jid, "local_unavailable", env.clock.now())
    r = await env.get(f"/v1/tasks/{jid}/result")
    assert r.json()["error"]["retryable"] is True
    env.worker.hold = False
    r2 = await env.post("/v1/chat/completions", body, headers=h, pump_s=2)
    assert r2.status_code == 200, r2.text


# --- FN-3: one Busy kills the local worker loop for good ---------------------------------------


async def test_worker_loop_survives_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    e = LocalEnv(tmp_path)
    w = e.worker
    calls = 0
    real_step = w.step

    async def flaky_step() -> float:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise Busy("database is locked")
        stop.set()
        return await real_step()

    monkeypatch.setattr(w, "step", flaky_step)
    stop = asyncio.Event()
    task = asyncio.ensure_future(w.run_forever(stop))
    for _ in range(50):
        await asyncio.sleep(0)
        if task.done():
            break
        nxt = e.clock.next_waiter()
        if nxt is not None:
            e.clock.advance_to(nxt)
    if not task.done():
        stop.set()
        e.clock.advance_to(e.clock.now() + 3600)
        await asyncio.wait_for(task, 5)
    assert task.exception() is None
    assert calls >= 2
