"""The pod runtime and the controller must not import each other."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _imports(base: Path) -> set[str]:
    names: set[str] = set()
    for f in base.rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names.add(node.module.split(".")[0])
    return names


def test_pod_does_not_import_controller() -> None:
    assert "llmhub" not in _imports(ROOT / "infer-image" / "llmhub_infer")


def test_controller_does_not_import_pod() -> None:
    assert "llmhub_infer" not in _imports(ROOT / "src" / "llmhub")
