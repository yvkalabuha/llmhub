"""T-369 H6 guard: no test/config/fixture may point at the real llama-server units."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REAL = re.compile(
    r"(?:127\.0\.0\.1|10\.8\.0\.1|localhost|0\.0\.0\.0)\s*:\s*809[0-2]\b|\b809[0-2]\b.{0,40}llama",
    re.IGNORECASE,
)
SELF = Path(__file__).name


def test_no_fixture_points_at_real_units() -> None:
    offenders: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.name == SELF or "__pycache__" in path.parts:
            continue
        if path.suffix not in {".py", ".yaml", ".yml", ".json", ".toml", ".env", ".txt", ".md"}:
            continue
        for n, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            if REAL.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:80]}")
    assert not offenders, "\n".join(offenders)


def test_guard_pattern_catches_real_addresses() -> None:
    for bad in (
        "http://127.0.0.1:8090/v1",
        "10.8.0.1:8092",
        "localhost:8091",
        "url: http://localhost: 8091",
    ):
        assert REAL.search(bad), bad
    for ok in ("http://fake-llama-a/v1", "127.0.0.1:8095", "127.0.0.1:18090", "port 80901"):
        assert not REAL.search(ok), ok


def test_fake_llama_has_no_socket_listener() -> None:
    src = (ROOT / "fake_llama.py").read_text()
    assert "uvicorn" not in src and "bind(" not in src and "serve(" not in src
