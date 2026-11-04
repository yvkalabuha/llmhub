"""T-373 A1: McpSettings from env."""

from __future__ import annotations

from pathlib import Path

import pytest

from llmhub.mcp_server.settings import McpSettings


def test_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    s = McpSettings.from_env({"HOME": str(tmp_path)})
    assert s.url == "" and s.key_env is None
    assert s.key_file == tmp_path / ".config/llmhub/key"
    assert s.scratch == tmp_path / ".local/state/llmhub-mcp/out"
    assert s.roots == () and s.kinds_file is None and s.allow_cloud is False
    assert s.max_input_bytes == 400_000 and s.max_inline_bytes == 16_000
    assert s.preview_bytes == 2_000 and s.default_wait_s == 90.0 and s.max_wait_s == 300.0
    assert s.http_timeout_s == 30.0 and s.default_model == "auto"


def test_roots_split_skip_empty_and_missing(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    env = {"LLMHUB_MCP_ROOTS": f"{a}::{tmp_path / 'missing'}:{b}:", "HOME": str(tmp_path)}
    assert McpSettings.from_env(env).roots == (a, b)


def test_values_from_env(tmp_path: Path) -> None:
    env = {
        "HOME": str(tmp_path), "LLMHUB_URL": "http://h:1/", "LLMHUB_KEY": " k ",
        "LLMHUB_KEY_FILE": str(tmp_path / "k"), "LLMHUB_MCP_SCRATCH": str(tmp_path / "s"),
        "LLMHUB_MCP_KINDS": str(tmp_path / "kinds.yaml"), "LLMHUB_MCP_MAX_INPUT_BYTES": "10",
        "LLMHUB_MCP_MAX_INLINE_BYTES": "20", "LLMHUB_MCP_PREVIEW_BYTES": "5",
        "LLMHUB_MCP_DEFAULT_WAIT_S": "1.5", "LLMHUB_MCP_MAX_WAIT_S": "2",
        "LLMHUB_MCP_HTTP_TIMEOUT_S": "3", "LLMHUB_MCP_MODEL": "big",
    }  # fmt: skip
    s = McpSettings.from_env(env)
    assert s.url == "http://h:1" and s.key_env == "k" and s.key_file == tmp_path / "k"
    assert s.scratch == tmp_path / "s" and s.kinds_file == tmp_path / "kinds.yaml"
    assert (s.max_input_bytes, s.max_inline_bytes, s.preview_bytes) == (10, 20, 5)
    assert (s.default_wait_s, s.max_wait_s, s.http_timeout_s) == (1.5, 2.0, 3.0)
    assert s.default_model == "big"


def test_empty_key_env_is_none(tmp_path: Path) -> None:
    assert McpSettings.from_env({"HOME": str(tmp_path), "LLMHUB_KEY": "  "}).key_env is None


@pytest.mark.parametrize("value", ["abc", "-1", "0", "nan", "1e999"])
def test_bad_number_raises_without_value(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError) as exc:
        McpSettings.from_env({"HOME": str(tmp_path), "LLMHUB_MCP_MAX_INPUT_BYTES": value})
    assert "LLMHUB_MCP_MAX_INPUT_BYTES" in str(exc.value) and value not in str(exc.value)


@pytest.mark.parametrize("value", ["abc", "-1", "inf"])
def test_bad_float_raises(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError):
        McpSettings.from_env({"HOME": str(tmp_path), "LLMHUB_MCP_MAX_WAIT_S": value})


@pytest.mark.parametrize(("value", "expected"), [("1", True), ("true", False), ("yes", False),
                                                 ("0", False), ("", False)])  # fmt: skip
def test_allow_cloud_only_one(tmp_path: Path, value: str, expected: bool) -> None:
    env = {"HOME": str(tmp_path), "LLMHUB_MCP_ALLOW_CLOUD": value}
    assert McpSettings.from_env(env).allow_cloud is expected
