"""T-373 E1: the MCP package imports only stdlib, its deps and itself; no print()."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "src" / "llmhub" / "mcp_server"
ALLOWED = {"httpx", "mcp", "mcp_types", "jsonschema", "yaml", "pydantic"}
FILES = sorted(PKG.glob("*.py"))


def test_package_files_found() -> None:
    names = {p.name for p in FILES}
    assert {"server.py", "service.py", "client.py", "paths.py", "__main__.py"} <= names


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_imports_allowed(path: Path) -> None:
    tree = ast.parse(path.read_text(), str(path))
    for node in ast.walk(tree):
        mods: list[str] = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{path.name}: relative import"
            mods = [node.module or ""]
        for mod in mods:
            top = mod.split(".")[0]
            if top == "llmhub":
                assert mod == "llmhub.mcp_server" or mod.startswith("llmhub.mcp_server."), mod
                continue
            assert top in sys.stdlib_module_names or top in ALLOWED, f"{path.name}: {mod}"
            assert top not in {"voicebot", "tools"}


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_print(path: Path) -> None:
    tree = ast.parse(path.read_text(), str(path))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "print"]  # fmt: skip
    assert calls == [], f"{path.name}: print() writes to the protocol channel"
    assert "sys.stdout" not in path.read_text()
