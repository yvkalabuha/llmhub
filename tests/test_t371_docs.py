"""T-371 G: runbook, integration guide and deploy README cover the deploy decisions."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = (ROOT / "docs/runbook.md").read_text()
INTEGRATION = (ROOT / "docs/integration.md").read_text()
DEPLOY = (ROOT / "deploy/README.md").read_text()


@pytest.mark.parametrize("text", [RUNBOOK, INTEGRATION, DEPLOY])
def test_starts_with_tldr(text: str) -> None:
    lines = text.splitlines()
    assert lines[0].startswith("# ") and lines[2].startswith("> TL;DR:")


def test_runbook_topics() -> None:
    for need in (
        "journalctl CONTAINER_NAME=llmhub-api", "--admin-key-file /dev/stdin",
        "clear-weights-hold", "db_corrupt", "75", "kill --file", "kill_incomplete",
        "tls_pin_mismatch", "low_memory", "llmhub-backup.timer", "s14 apply", "e2e",
        "Mock-ledger", "64100", "/w/000000000000/qwen/x.gguf",
    ):  # fmt: skip
        assert need in RUNBOOK, need
    # docker logs is not the way (journald log driver)
    assert "`docker logs` не используется" in RUNBOOK
    assert not re.search(r"^\s*docker logs\b", RUNBOOK, re.MULTILINE)


def test_integration_proxy_paragraph_and_limits() -> None:
    for need in ("proxy_read_timeout", "proxy_buffering off", "proxy_request_buffering off",
                 "429 rate_limited", "Retry-After", "10.8.0.1:8095", "docs/openapi.yaml",
                 "C#/.NET", "T-366", "`/docs`", "max_active_jobs"):  # fmt: skip
        assert need in INTEGRATION, need
    assert "| `rate_limited` | 429 | да |" in INTEGRATION


def test_integration_mcp_registration_is_valid_json_without_keys() -> None:
    block = re.search(r"### Регистрация.*?```json\n(.*?)```", INTEGRATION, re.DOTALL)
    assert block
    data = json.loads(block.group(1))
    srv = data["mcpServers"]["llmhub"]
    assert srv["timeout"] == 360000
    assert "LLMHUB_KEY_FILE" in srv["env"] and "LLMHUB_KEY" not in srv["env"]
    assert "LLMHUB_MCP_ALLOW_CLOUD" not in srv["env"]


def test_deploy_readme_layout() -> None:
    for need in ("llmhub-api", "llmhub-reaper", "llmhub-weights-auth", "llmhub-weights-nginx",
                 "--network host", "64100", "64101", "64102", "llmhub-deploy.sh",
                 "F13", "F12", "s10x", "test_t371_deploy_script.py"):  # fmt: skip
        assert need in DEPLOY, need
    assert "www-data" not in DEPLOY.split("## Таймауты")[0]
