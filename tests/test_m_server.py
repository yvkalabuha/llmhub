"""T-373 E2/E3: MCP tools in-process (mcp Client) on the ApiEnv gateway; stdio entry point."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import Client

from api_fixtures import GAME_KEY, ApiEnv
from api_gateway_stubs import completion
from llmhub.mcp_server.server import build_server
from m_fixtures import make_settings


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[ApiEnv]:
    (tmp_path / "hub").mkdir()
    e = ApiEnv(tmp_path / "hub")
    yield e
    await e.aclose()


def server_for(env: ApiEnv, tmp: Path, **over: Any) -> Any:
    settings = make_settings(tmp, key_env=GAME_KEY, **over)
    transport = httpx.ASGITransport(app=env.app, raise_app_exceptions=False)
    return build_server(settings, transport=transport, sleep=env.pump, monotonic=env.clock.now)


async def test_tools_schemas_annotations(env: ApiEnv, tmp_path: Path) -> None:
    async with Client(server_for(env, tmp_path)) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == {"llmhub_ask", "llmhub_result", "llmhub_cancel", "llmhub_models"}
    ask = tools["llmhub_ask"]
    props = ask.input_schema["properties"]
    assert props["data_class"]["enum"] == ["private", "cloud_allowed"]
    assert props["data_class"]["default"] == "private"
    assert props["kind"]["default"] == "general" and props["overwrite"]["default"] is False
    assert props["fresh"]["default"] is False and props["priority"]["anyOf"]
    assert ask.input_schema.get("required", []) == []
    desc = ask.description or ""
    for needle in ("schema", "classif", "verdict", "yourself", "secret", "MCP_TOOL_TIMEOUT",
                   "extract_json", "Pilot"):  # fmt: skip
        assert needle in desc, needle
    assert ask.annotations and ask.annotations.open_world_hint is True
    assert ask.annotations.read_only_hint is False
    models = tools["llmhub_models"]
    assert models.annotations and models.annotations.read_only_hint is True
    cancel = tools["llmhub_cancel"]
    assert cancel.annotations and cancel.annotations.destructive_hint is True
    assert tools["llmhub_result"].input_schema["required"] == ["task_id"]


async def test_ask_ok_structured(env: ApiEnv, tmp_path: Path) -> None:
    env.worker.answer = completion('["a", "b"]')
    async with Client(server_for(env, tmp_path)) as c:
        r = await c.call_tool("llmhub_ask", {"kind": "classify", "input": "x",
                                             "checks": ["item-count:2"], "wait_s": 20})  # fmt: skip
    assert r.is_error is False
    data = r.structured_content
    assert data["verdict"] == "ok" and data["text"] == '["a", "b"]'
    assert data["backend"] == "local:cpu" and data["task_id"]
    assert [c["name"] for c in data["checks"]] == ["not-truncated", "item-count"]


async def test_error_is_not_tool_error(env: ApiEnv, tmp_path: Path) -> None:
    async with Client(server_for(env, tmp_path)) as c:
        r = await c.call_tool("llmhub_ask", {"input": "x", "output_file": "/etc/passwd"})
        cloud = await c.call_tool("llmhub_ask", {"input": "x", "data_class": "cloud_allowed"})
    assert r.is_error is False
    assert r.structured_content["verdict"] == "error"
    assert r.structured_content["error"]["code"] == "outside_roots"
    assert cloud.structured_content["error"]["code"] == "cloud_not_enabled"


async def test_pending_result_cancel_models(env: ApiEnv, tmp_path: Path) -> None:
    env.worker.hold = True
    async with Client(server_for(env, tmp_path)) as c:
        p = (await c.call_tool("llmhub_ask", {"input": "x", "wait_s": 0})).structured_content
        assert p["verdict"] == "pending"
        env.worker.hold = False
        r = await c.call_tool("llmhub_result", {"task_id": p["task_id"], "wait_s": 10})
        assert r.structured_content["verdict"] == "ok"
        env.worker.hold = True
        p2 = (await c.call_tool("llmhub_ask", {"input": "y", "wait_s": 0})).structured_content
        cancel = await c.call_tool("llmhub_cancel", {"task_id": p2["task_id"]})
        assert cancel.structured_content["status"] == "cancelled"
        models = (await c.call_tool("llmhub_models", {})).structured_content
    assert models["error"] is None and "auto" in {m["id"] for m in models["models"]}
    assert len(models["kinds"]) == 6


async def test_bad_argument_type_is_rejected(env: ApiEnv, tmp_path: Path) -> None:
    async with Client(server_for(env, tmp_path)) as c:
        r = await c.call_tool("llmhub_ask", {"input": "x", "data_class": "public"})
    assert r.is_error is True


def test_main_bad_env_exits_with_stderr_and_clean_stdout(tmp_path: Path) -> None:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LLMHUB_MCP_MAX_INPUT_BYTES": "lots"}
    proc = subprocess.run(
        [sys.executable, "-m", "llmhub.mcp_server"], env=env, capture_output=True, text=True,
        timeout=60, stdin=subprocess.DEVNULL, check=False,
    )  # fmt: skip
    assert proc.returncode != 0 and proc.stdout == ""
    assert "LLMHUB_MCP_MAX_INPUT_BYTES" in proc.stderr and "lots" not in proc.stderr


def test_setup_logging_levels() -> None:
    import logging

    from llmhub.mcp_server.__main__ import setup_logging

    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        setup_logging()
        assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
        assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING
        assert all(getattr(h, "stream", None) is sys.stderr for h in root.handlers)
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
