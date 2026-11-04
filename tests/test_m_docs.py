"""T-373 E5: integration.md §8 documents MCP registration, variables and timeouts."""

from __future__ import annotations

import json
import re
from pathlib import Path

DOC = Path(__file__).resolve().parents[1] / "docs" / "integration.md"


def _section() -> str:
    text = DOC.read_text()
    assert "## 8. MCP для агентов" in text
    return text.split("## 8. MCP для агентов", 1)[1]


def test_tldr_mentions_mcp() -> None:
    first = DOC.read_text().splitlines()[2]
    assert first.startswith("> TL;DR") and "MCP" in first


def test_registration_example_parses_and_has_no_key_value() -> None:
    sec = _section()
    blocks = re.findall(r"```json\n(.*?)\n```", sec, re.DOTALL)
    assert blocks
    cfg = json.loads(blocks[0])
    srv = cfg["mcpServers"]["llmhub"]
    assert srv["type"] == "stdio" and "llmhub.mcp_server" in srv["args"]
    env = srv["env"]
    assert "LLMHUB_KEY_FILE" in env and "LLMHUB_KEY" not in env
    assert '"LLMHUB_KEY"' not in sec and "lh_" not in blocks[0]
    assert {"LLMHUB_URL", "LLMHUB_MCP_ROOTS", "LLMHUB_MCP_SCRATCH"} <= set(env)


def test_timeouts_and_rules_documented() -> None:
    sec = _section()
    for needle in ("MCP_TOOL_TIMEOUT", "wait_s", "pending", "llmhub_result",
                   "LLMHUB_MCP_MAX_INLINE_BYTES", "LLMHUB_MCP_MAX_INPUT_BYTES",
                   "LLMHUB_MCP_ALLOW_CLOUD", "cloud_allowed=false", "Нельзя", "Эскалация"):  # fmt: skip
        assert needle in sec, needle
