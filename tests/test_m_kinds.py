"""T-373 B2: task kinds (built-in kinds.yaml + user file)."""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path

import pytest

from llmhub.mcp_server import kinds as kinds_mod
from llmhub.mcp_server.kinds import BUILTIN, Kind, KindsError, load_kinds

NAMES = {"extract_json", "classify", "summarize", "translate", "review", "general"}


def test_builtin_resource_is_packaged() -> None:
    assert resources.files("llmhub.mcp_server").joinpath(BUILTIN).is_file()


def test_builtin_six_kinds() -> None:
    kinds = load_kinds(None)
    assert set(kinds) == NAMES
    assert all(isinstance(k, Kind) and k.task_type is None for k in kinds.values())
    assert kinds["extract_json"].requires_schema and kinds["extract_json"].think is False
    assert kinds["extract_json"].max_tokens == 2000 and kinds["classify"].max_tokens == 1000
    assert kinds["review"].think is True and kinds["general"].think is None
    for name in ("summarize", "translate", "review", "general"):
        assert "Pilot" in kinds[name].description
    assert all(k.system and k.description for k in kinds.values())


def test_user_file_adds_and_overrides(tmp_path: Path) -> None:
    f = tmp_path / "kinds.yaml"
    f.write_text(
        "classify:\n  max_tokens: 50\n"
        "commits:\n  description: d\n  system: s\n  max_tokens: 10\n"
        "  checks: ['max-bytes:100']\n  task_type: summarize\n"
    )
    f.chmod(0o644)
    kinds = load_kinds(f)
    assert kinds["classify"].max_tokens == 50 and kinds["classify"].think is False
    assert kinds["classify"].system == load_kinds(None)["classify"].system
    assert kinds["commits"] == Kind(
        name="commits", description="d", system="s", max_tokens=10, checks=("max-bytes:100",),
        task_type="summarize",
    )  # fmt: skip


@pytest.mark.parametrize("text", [
    "general:\n  colour: red\n",
    "new:\n  system: s\n",                      # missing required fields for a new kind
    "general:\n  max_tokens: -1\n",
    "general:\n  max_tokens: many\n",
    "general:\n  checks: 'max-bytes:1'\n",      # must be a list
    "general:\n  checks: ['bogus']\n",          # bad check spec
    "general:\n  think: maybe\n",
    "- a\n- b\n",
    "BAD NAME:\n  description: d\n  system: s\n  max_tokens: 1\n",
])  # fmt: skip
def test_user_file_errors(tmp_path: Path, text: str) -> None:
    f = tmp_path / "kinds.yaml"
    f.write_text(text)
    f.chmod(0o644)
    with pytest.raises(KindsError):
        load_kinds(f)


@pytest.mark.parametrize("mode", [0o664, 0o646, 0o666])
def test_user_file_writable_by_others_refused(tmp_path: Path, mode: int) -> None:
    f = tmp_path / "kinds.yaml"
    f.write_text("classify:\n  max_tokens: 50\n")
    f.chmod(mode)
    with pytest.raises(KindsError, match="owned by the current user"):
        load_kinds(f)


def test_user_file_foreign_owner_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "kinds.yaml"
    f.write_text("classify:\n  max_tokens: 50\n")
    f.chmod(0o644)
    real = os.getuid()
    monkeypatch.setattr(kinds_mod.os, "getuid", lambda: real + 1)
    with pytest.raises(KindsError, match="owned by the current user"):
        load_kinds(f)


def test_user_file_not_regular_refused(tmp_path: Path) -> None:
    f = tmp_path / "kinds.fifo"
    os.mkfifo(f, 0o600)
    with pytest.raises(KindsError, match="regular"):
        load_kinds(f)


def test_missing_user_file(tmp_path: Path) -> None:
    with pytest.raises(KindsError):
        load_kinds(tmp_path / "none.yaml")
