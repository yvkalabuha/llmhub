"""T-369 I4 (slow): the real app on uvicorn with real sockets, driven by openai-python.

Covers: chat (local and cloud Mock), stream assembled by the SDK (tool_calls with index,
reasoning_content), 403 -> PermissionDeniedError, SSE error event -> APIError, client
disconnect seen by the shared poller (`request.is_disconnected()` from another task) without
cancelling the job, and `python -m llmhub serve` as a process (SIGHUP reload, SIGTERM stop).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest
import uvicorn
import yaml

from api_fixtures import chat_body
from int_live import (
    ADMIN_KEY,
    GAME_KEY,
    LOW_KEY,
    LiveStack,
    RichLlama,
    free_port,
    run_serve_process,
)
from llmhub.api_gateway.auth import hash_key

pytestmark = pytest.mark.slow

TOOLS = [{"type": "function", "function": {"name": "get_weather", "parameters": {
    "type": "object", "properties": {"city": {"type": "string"}}}}}]  # fmt: skip
TOOL_CALLS = [
    {"id": "call_1", "type": "function",
     "function": {"name": "get_weather", "arguments": "{\"city\": \"Oslo\"}"}},
    {"id": "call_2", "type": "function",
     "function": {"name": "get_weather", "arguments": "{\"city\": \"Rome\"}"}},
]  # fmt: skip


@pytest.fixture
def live(tmp_path: Path) -> Iterator[LiveStack]:
    stack = LiveStack(tmp_path).start()
    yield stack
    stack.stop()


def sdk(live: LiveStack, key: str = GAME_KEY, **kw: Any) -> openai.OpenAI:
    return openai.OpenAI(base_url=live.url + "/v1", api_key=key, timeout=60, **kw)


def test_chat_local_and_cloud(live: LiveStack) -> None:
    client = sdk(live)
    raw = client.chat.completions.with_raw_response.create(
        model="localonly", messages=[{"role": "user", "content": "hi"}]
    )
    r = raw.parse()
    assert r.choices[0].message.content == "hello" and r.model == "localonly"
    assert raw.headers["x-llmhub-backend"] == "local:cpu"
    cloud = client.chat.completions.with_raw_response.create(
        model="cloudonly", messages=[{"role": "user", "content": "hi"}], max_tokens=50
    )
    assert cloud.parse().choices[0].message.content == "ok"
    assert cloud.headers["x-llmhub-backend"] == "cloud:p1"
    models = {m.id for m in client.models.list()}
    assert {"big", "cloudonly", "localonly", "auto"} <= models


def test_stream_tool_calls_and_reasoning(live: LiveStack) -> None:
    live.llama.tool_calls = TOOL_CALLS
    live.llama.reasoning = "thinking..."
    client = sdk(live)
    stream = client.chat.completions.create(
        model="localonly", messages=[{"role": "user", "content": "weather?"}], tools=TOOLS,  # type: ignore[arg-type]
        stream=True, stream_options={"include_usage": True},
    )  # fmt: skip
    calls: dict[int, dict[str, str]] = {}
    reasoning, finish, usage = "", None, None
    for chunk in stream:
        if chunk.usage is not None:
            usage = chunk.usage
        for ch in chunk.choices:
            d = ch.delta
            reasoning += getattr(d, "reasoning_content", None) or ""
            for tc in d.tool_calls or []:
                slot = calls.setdefault(tc.index, {"name": "", "args": ""})
                if tc.function is not None:
                    slot["name"] += tc.function.name or ""
                    slot["args"] += tc.function.arguments or ""
            finish = ch.finish_reason or finish
    assert reasoning == "thinking..."
    assert finish == "tool_calls"
    assert [calls[i]["name"] for i in sorted(calls)] == ["get_weather", "get_weather"]
    assert [json.loads(calls[i]["args"])["city"] for i in sorted(calls)] == ["Oslo", "Rome"]
    assert usage is not None and usage.total_tokens == 140
    # the same answer without stream: the SDK parses tool_calls
    msg = (
        client.chat.completions.create(
            model="localonly",
            messages=[{"role": "user", "content": "weather 2?"}],
            tools=TOOLS,  # type: ignore[arg-type]
        )
        .choices[0]
        .message
    )
    assert msg.tool_calls is not None and [t.id for t in msg.tool_calls] == ["call_1", "call_2"]


def test_forbidden_model_is_permission_denied(live: LiveStack) -> None:
    with pytest.raises(openai.PermissionDeniedError) as ei:
        sdk(live, LOW_KEY, max_retries=0).chat.completions.create(
            model="big", messages=[{"role": "user", "content": "x"}]
        )
    assert ei.value.status_code == 403
    body: Any = ei.value.body
    assert body["code"] == "forbidden"


def test_sse_error_event_raises_api_error(live: LiveStack) -> None:
    live.llama.delays.append(4.0)
    stream = sdk(live).chat.completions.create(
        model="localonly", messages=[{"role": "user", "content": "slow"}], stream=True,
        extra_headers={"X-LLMHub-Wait": "1"},
    )  # fmt: skip
    with pytest.raises(openai.APIError) as ei:
        for _ in stream:
            pass
    assert "task" in str(ei.value).lower() or "wait" in str(ei.value).lower()


def test_disconnect_seen_by_poller_task_keeps_running(live: LiveStack) -> None:
    """OPEN item of stream W: `request.is_disconnected()` is called by the shared poller
    (another asyncio task) on live uvicorn; the waiter leaves, the job is not cancelled."""
    live.llama.delays.append(5.0)
    with live.client() as c, pytest.raises(httpx.ReadTimeout):
        c.post("/v1/chat/completions", json=chat_body("bye", model="localonly"), timeout=1.0)
    t0 = time.monotonic()
    live.wait_for(lambda: live.waiters() == 0, timeout=4.0)
    assert time.monotonic() - t0 < 4.0  # well before the 5 s answer
    (job,) = live.query("SELECT id, status, cancel_requested FROM jobs")
    assert job["status"] in ("queued", "running") and job["cancel_requested"] == 0
    live.wait_for(lambda: live.job(job["id"])["status"] == "completed", timeout=15)
    with live.client() as c:
        res = c.get(f"/v1/tasks/{job['id']}/result")
    assert res.status_code == 200 and res.json()["choices"][0]["message"]["content"] == "hello"


def test_stream_disconnect_keeps_job(live: LiveStack) -> None:
    live.llama.delays.append(4.0)
    with (
        live.client() as c,
        c.stream(
            "POST", "/v1/chat/completions", json=chat_body("s", model="localonly", stream=True)
        ) as resp,
    ):
        assert resp.status_code == 200
        first = next(resp.iter_lines())
        assert first.startswith(": keep-alive")
    live.wait_for(lambda: live.waiters() == 0, timeout=4.0)
    (job,) = live.query("SELECT id, status FROM jobs")
    live.wait_for(lambda: live.job(job["id"])["status"] == "completed", timeout=15)


# --- python -m llmhub serve as a process ----------------------------------------------------
def _serve_config(tmp: Path, port: int, llama_port: int, clients: dict[str, Any]) -> Path:
    cfg = {
        "data_dir": str(tmp / "data"),
        "pools": {"p1": {"provider": "mock", "gpu": "RTX4090", "vram_gb": 24, "pp_tps": 1200,
                         "tg_tps": 78, "image": "llmhub-infer:test", "disk_gb": 50,
                         "models": ["big"]}},
        "local_backends": {"cpu": {"url": f"http://127.0.0.1:{llama_port}/v1",
                                   "model": "local-model", "ctx_tokens": 32768, "pp_tps": 50,
                                   "tg_tps": 5}},
        "models": {"big": {"local": "cpu"}},
        "local": {"probe_interval_s": 1, "poll_s": 0.2},
        "clients": clients,
        "api": {"host": "127.0.0.1", "port": port, "admin_key_sha256": hash_key(ADMIN_KEY),
                "wait_poll_s": 0.1},
    }  # fmt: skip
    path = tmp / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


def _wait_http(url: str, proc: subprocess.Popen[str], timeout: float = 30) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if proc.poll() is not None:
            raise AssertionError(f"serve exited early: {proc.stdout.read() if proc.stdout else ''}")
        try:
            if httpx.get(url, timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise AssertionError("serve did not come up")


def test_serve_process_sighup_and_sigterm(tmp_path: Path) -> None:
    import asyncio
    import threading

    llama = RichLlama()
    llama_port, port = free_port(), free_port()
    srv = uvicorn.Server(uvicorn.Config(llama, host="127.0.0.1", port=llama_port,
                                        log_config=None, lifespan="off"))  # fmt: skip
    th = threading.Thread(target=lambda: asyncio.run(srv.serve()), daemon=True)
    th.start()
    game = {"key_sha256": hash_key(GAME_KEY), "allowed_models": ["*"]}
    path = _serve_config(tmp_path, port, llama_port, {"game": game})
    env = {k: v for k, v in os.environ.items() if not k.startswith("LLMHUB_")}
    env["LLMHUB_LABEL_SECRET"] = "x" * 40
    proc = run_serve_process(path, env)
    try:
        url = f"http://127.0.0.1:{port}"
        _wait_http(url + "/healthz", proc)
        client = openai.OpenAI(base_url=url + "/v1", api_key=GAME_KEY, timeout=30)
        r = client.chat.completions.create(model="big", messages=[{"role": "user", "content": "x"}])
        assert r.choices[0].message.content == "hello"
        # SIGHUP: a client added to the file works without a restart
        new_key = "lh_test_new_client_key_00000000"
        cfg = yaml.safe_load(path.read_text())
        cfg["clients"]["newbie"] = {"key_sha256": hash_key(new_key), "allowed_models": ["big"]}
        path.write_text(yaml.safe_dump(cfg))
        before = httpx.get(url + "/v1/models", headers={"Authorization": f"Bearer {new_key}"})
        assert before.status_code == 401
        os.kill(proc.pid, signal.SIGHUP)
        end = time.monotonic() + 10
        status = 401
        while time.monotonic() < end and status != 200:
            status = httpx.get(
                url + "/v1/models", headers={"Authorization": f"Bearer {new_key}"}
            ).status_code
            time.sleep(0.1)
        assert status == 200
        # SIGTERM (own PID only): graceful exit
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20) == 0
        out = proc.stdout.read() if proc.stdout else ""
        assert GAME_KEY not in out and new_key not in out and "x" * 40 not in out
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
        srv.should_exit = True
        th.join(10)
