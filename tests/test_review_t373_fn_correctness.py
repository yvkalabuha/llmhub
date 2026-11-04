"""T-373 review cycle 2 (correctness & reliability): probes of the MCP server.

Confirmed defects are `xfail(strict=True)`: they turn into failures once fixed, so the fix
round must drop the marker. Passing tests pin behaviour the review relied on.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import Client

from api_fixtures import GAME_KEY
from api_gateway_stubs import completion
from llmhub.mcp_server import paths as paths_mod
from llmhub.mcp_server.client import HubClient
from llmhub.mcp_server.kinds import load_kinds
from llmhub.mcp_server.paths import Roots
from llmhub.mcp_server.secret import Secret
from llmhub.mcp_server.server import build_server
from llmhub.mcp_server.service import AskParams, McpService
from m_fixtures import Hub, make_settings


@pytest.fixture
async def hub(tmp_path: Path) -> AsyncIterator[Hub]:
    h = Hub(tmp_path)
    yield h
    await h.aclose()


# --- task_id after submit ------------------------------------------------------------------
async def test_output_race_after_submit_keeps_task_id(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = hub.root / "late.txt"
    real = paths_mod.Roots.write_text

    def racing(self: Any, path: Path, text: str, *, overwrite: bool) -> None:
        if path == out.resolve():
            out.write_text("someone")
        real(self, path, text, overwrite=overwrite)

    monkeypatch.setattr(paths_mod.Roots, "write_text", racing)
    hub.worker.answer = completion("answer")
    r = await hub.service.ask(AskParams(input="x", output_file=str(out), wait_s=9))
    assert r.verdict == "error" and r.error and r.error.code == "exists"
    assert r.task_id is not None  # the paid answer must stay reachable via llmhub_result


async def test_unresolvable_ref_schema_refused_before_submit(hub: Hub) -> None:
    hub.worker.answer = completion("{}")
    r = await hub.service.ask(
        AskParams(kind="extract_json", input="x", schema={"$ref": "#/$defs/missing"}, wait_s=9)
    )
    assert r.error is not None and r.error.code != "internal_error"
    assert hub.transport.requests == [], "a broken schema must be refused before submit"


async def test_deeply_nested_answer_is_check_failed_with_task_id(hub: Hub) -> None:
    hub.worker.answer = completion("[" * 100_000 + "]" * 100_000)
    r = await hub.service.ask(
        AskParams(kind="classify", input="x", schema={"type": "array"}, wait_s=9)
    )
    assert r.verdict == "check_failed" and r.task_id is not None


async def test_retryable_status_error_keeps_task_id_and_rejoins(hub: Hub) -> None:
    """Pending -> same ask again joins the task (no second job), result is reachable."""
    hub.worker.hold = True
    p = AskParams(input="same", wait_s=0)
    first = await hub.service.ask(p)
    again = await hub.service.ask(p)
    assert first.verdict == again.verdict == "pending"
    assert first.task_id == again.task_id
    hub.worker.hold = False
    hub.worker.answer = completion("done")
    r = await hub.service.result(first.task_id or "", wait_s=9)
    assert r.verdict == "ok" and r.text == "done"


async def test_cancelled_then_result_is_error_with_task_id(hub: Hub) -> None:
    hub.worker.hold = True
    p = await hub.service.ask(AskParams(input="c", wait_s=0))
    assert p.task_id
    c = await hub.service.cancel(p.task_id)
    assert c.status == "cancelled"
    r = await hub.service.result(p.task_id)
    assert r.verdict == "error" and r.task_id == p.task_id and r.error
    assert r.error.code == "cancelled" and r.error.retryable is False


# --- wait budget vs tool timeout -----------------------------------------------------------
class SlowHub:
    """MockTransport whose every request costs `cost_s` on a manual clock. MockTransport ignores
    httpx timeouts, so the handler honours the per-request read timeout itself: a request that
    would take longer advances the clock by the timeout and raises ReadTimeout (fix round 1)."""

    def __init__(self, cost_s: float, done_at: float) -> None:
        self.t = 0.0
        self.cost_s = cost_s
        self.done_at = done_at

    def now(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += s

    def handler(self, req: httpx.Request) -> httpx.Response:
        limit = req.extensions.get("timeout", {}).get("read")
        if limit is not None and self.cost_s > limit:
            self.t += limit
            raise httpx.ReadTimeout("slow", request=req)
        self.t += self.cost_s
        if req.method == "POST":
            return httpx.Response(202, json={"id": "t1", "status": "queued"})
        if req.url.path.endswith("/result"):
            return httpx.Response(200, content=completion("ok"),
                                  headers={"content-type": "application/json"})  # fmt: skip
        status = "completed" if self.t >= self.done_at else "running"
        return httpx.Response(200, json={"id": "t1", "status": status, "eta_s": 10})


async def _slow_ask(tmp_path: Path, slow: SlowHub, wait_s: float) -> Any:
    settings = make_settings(tmp_path)
    client = HubClient(
        "http://t", lambda: Secret(GAME_KEY), timeout_s=30,
        transport=httpx.MockTransport(slow.handler), sleep=slow.sleep, monotonic=slow.now,
    )  # fmt: skip
    svc = McpService(settings, client, Roots.build(settings.roots, settings.scratch),
                     load_kinds(None))  # fmt: skip
    try:
        return await svc.ask(AskParams(input="x", wait_s=wait_s))
    finally:
        await client.aclose()


async def test_call_duration_bounded_by_wait_plus_margin(tmp_path: Path) -> None:
    """Fixed in round 1 (C2-F4): one deadline per call. With 25 s per request the answer is not
    reachable within wait_s=90, so the call ends as pending (task_id kept) within
    wait_s + 20 s instead of running to 130 s. Originally asserted `ok`, which no bounded
    schedule can deliver here (a completed poll needs to start >= 64 s, then /result 25 s)."""
    slow = SlowHub(cost_s=25.0, done_at=89.0)  # each request < http_timeout_s=30
    r = await _slow_ask(tmp_path, slow, 90)
    assert slow.t <= 90 + 20, f"call took {slow.t:.0f} s"
    assert r.verdict == "pending" and r.task_id == "t1"


async def test_call_with_fast_requests_still_ok(tmp_path: Path) -> None:
    slow = SlowHub(cost_s=1.0, done_at=60.0)
    r = await _slow_ask(tmp_path, slow, 90)
    assert r.verdict == "ok" and r.text == "ok" and slow.t <= 90 + 20


async def test_wait_zero_with_hung_server_is_bounded(tmp_path: Path) -> None:
    slow = SlowHub(cost_s=1000.0, done_at=0.0)  # every request hangs past the HTTP timeout
    r = await _slow_ask(tmp_path, slow, 0)
    assert slow.t <= 5 + 20, f"call took {slow.t:.0f} s"
    assert r.verdict == "error" and r.error and r.error.code == "deadline"


# --- MCP layer: server survives bad arguments ---------------------------------------------
async def test_server_survives_bad_args_and_keeps_serving(tmp_path: Path) -> None:
    from api_fixtures import ApiEnv

    (tmp_path / "hub").mkdir()
    env = ApiEnv(tmp_path / "hub")
    try:
        settings = make_settings(tmp_path, key_env=GAME_KEY)
        transport = httpx.ASGITransport(app=env.app, raise_app_exceptions=False)
        server = build_server(settings, transport=transport, sleep=env.pump,
                              monotonic=env.clock.now)  # fmt: skip
        env.worker.answer = completion("fine")
        async with Client(server) as c:
            bad = [
                await c.call_tool("llmhub_ask", {"input": "x", "wait_s": -1}),
                await c.call_tool("llmhub_ask", {"input": "x", "max_tokens": 0}),
                await c.call_tool("llmhub_ask", {"input": 5}),
                await c.call_tool("llmhub_ask", {"input": "x", "checks": "regex:a"}),
                await c.call_tool("llmhub_result", {}),
                await c.call_tool("llmhub_cancel", {"task_id": None}),
            ]
            soft = [
                await c.call_tool("llmhub_ask", {"input": "x", "checks": ["regex:("]}),
                await c.call_tool("llmhub_ask", {"input": "x", "kind": "nope"}),
                await c.call_tool("llmhub_ask", {"input": "x", "input_files": ["rel.txt"]}),
                await c.call_tool("llmhub_result", {"task_id": "../../admin"}),
                await c.call_tool("llmhub_cancel", {"task_id": "missing"}),
            ]
            ok = await c.call_tool("llmhub_ask", {"input": "x", "wait_s": 9})
    finally:
        await env.aclose()
    assert all(r.is_error for r in bad)
    codes = [r.structured_content["error"]["code"] for r in soft if r.structured_content]
    assert not any(r.is_error for r in soft)
    assert codes == ["invalid_check", "invalid_request", "path_not_absolute", "not_found",
                     "not_found"]  # fmt: skip
    assert ok.structured_content and ok.structured_content["verdict"] == "ok"
    assert json.dumps(ok.structured_content).count(GAME_KEY) == 0
