"""T-373 E4 (slow): the MCP server as a stdio subprocess against the live app (LiveStack)."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

from int_live import GAME_KEY, LiveStack

pytestmark = pytest.mark.slow


@pytest.fixture
def live(tmp_path: Path) -> Iterator[LiveStack]:
    data = tmp_path / "hub_data"
    data.mkdir()
    stack = LiveStack(data).start()
    yield stack
    stack.stop()


def params(live: LiveStack, tmp: Path) -> StdioServerParameters:
    key = tmp / "agents.key"
    key.write_text(GAME_KEY + "\n")
    key.chmod(0o600)
    root = tmp / "proj"
    root.mkdir()
    env = {
        "LLMHUB_URL": live.url, "LLMHUB_KEY_FILE": str(key), "LLMHUB_MCP_ROOTS": str(root),
        "LLMHUB_MCP_SCRATCH": str(tmp / "scratch"), "HOME": str(tmp),
        "PATH": os.environ.get("PATH", ""),
    }  # fmt: skip
    return StdioServerParameters(command=sys.executable, args=["-m", "llmhub.mcp_server"],
                                 env=env, cwd=str(tmp))  # fmt: skip


async def test_stdio_ok_check_failed_pending_result(live: LiveStack, tmp_path: Path) -> None:
    live.llama.text = '["alpha", "beta"]'
    errlog_path = tmp_path / "mcp-stderr.log"
    with errlog_path.open("w") as errlog:
        async with Client(stdio_client(params(live, tmp_path), errlog=errlog)) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert names == {"llmhub_ask", "llmhub_result", "llmhub_cancel", "llmhub_models"}

            ok = (await c.call_tool("llmhub_ask", {
                "kind": "classify", "input": "label a and b", "checks": ["item-count:2"],
                "wait_s": 30})).structured_content  # fmt: skip
            assert ok["verdict"] == "ok", ok
            assert ok["text"] == '["alpha", "beta"]' and ok["backend"] == "local:cpu"

            bad = (await c.call_tool("llmhub_ask", {
                "input": "other", "checks": ["regex:^gamma$"], "wait_s": 30,
            })).structured_content  # fmt: skip
            assert bad["verdict"] == "check_failed", bad
            assert bad["output_file"].endswith(".rejected.txt")

            live.llama.delays.append(3.0)
            pend = await c.call_tool("llmhub_ask", {"input": "slow one", "wait_s": 0})
            pend_data = pend.structured_content
            assert pend_data["verdict"] == "pending" and pend_data["task_id"], pend_data
            done = (await c.call_tool("llmhub_result", {
                "task_id": pend_data["task_id"], "wait_s": 30})).structured_content  # fmt: skip
            assert done["verdict"] == "ok" and done["text"] == '["alpha", "beta"]', done

            models = (await c.call_tool("llmhub_models", {})).structured_content
            assert models["error"] is None and models["models"]
    stderr = errlog_path.read_text()
    assert GAME_KEY not in stderr and "Bearer" not in stderr
    assert live.job(pend_data["task_id"])["data_class"] == "private"
