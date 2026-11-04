"""T-370 stream C4: static checks of infer-image (no docker needed)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

IMG = Path(__file__).resolve().parents[1] / "infer-image"
ROOT = IMG.parent


def lines() -> list[str]:
    """Dockerfile instructions with continuation lines joined, comments dropped."""
    out: list[str] = []
    buf = ""
    for raw in (IMG / "Dockerfile").read_text().splitlines():
        if raw.strip().startswith("#"):
            continue
        if raw.rstrip().endswith("\\"):
            buf += raw.rstrip()[:-1] + " "
            continue
        out.append((buf + raw).strip())
        buf = ""
    return [x for x in out if x]


def test_from_pinned_by_digest() -> None:
    text = (IMG / "Dockerfile").read_text()
    m = re.search(r"^ARG LLAMA_IMAGE=(\S+)$", text, re.MULTILINE)
    assert m and re.search(r"@sha256:[0-9a-f]{64}$", m.group(1))
    assert "server-cuda12-b12063" in m.group(1)
    froms = [x for x in lines() if x.startswith("FROM ")]
    assert froms == ["FROM ${LLAMA_IMAGE}"]


def test_no_secret_env_or_arg() -> None:
    for x in lines():
        if x.startswith(("ENV ", "ARG ")):
            assert not re.search(r"(TOKEN|KEY|SECRET|PASSWORD)", x, re.IGNORECASE), x


def test_no_weights_no_remote_add() -> None:
    for x in lines():
        if x.startswith(("COPY ", "ADD ")):
            assert ".gguf" not in x and "http" not in x, x
        assert not x.startswith("ADD http")
    ignore = (IMG / ".dockerignore").read_text()
    for pat in ("*.gguf", ".env", "secrets/", "**/__pycache__"):
        assert pat in ignore


def test_healthcheck_none_and_entrypoint() -> None:
    ls = lines()
    assert "HEALTHCHECK NONE" in ls
    ep = [x for x in ls if x.startswith("ENTRYPOINT ")]
    assert ep and "llmhub_infer" in ep[0] and "/opt/llmhub/venv/bin/python" in ep[0]


def test_apt_python_pinned_or_explicitly_accepted() -> None:
    text = (IMG / "Dockerfile").read_text()
    pinned = re.search(r"python3\.12=\S+", text) and re.search(r"python3\.12-venv=\S+", text)
    accepted = "APT-PIN-ACCEPTED:" in text
    assert pinned or accepted
    run = " ".join(x for x in lines() if x.startswith("RUN ") and "apt-get install" in x)
    assert "--no-install-recommends" in run and "rm -rf /var/lib/apt/lists" in run


def test_requirements_hashed_and_match_lock() -> None:
    text = (IMG / "requirements.txt").read_text()
    reqs = re.findall(r"^([A-Za-z0-9_.-]+)==(\S+) \\$", text, re.MULTILINE)
    assert reqs
    for name in (n for n, _ in reqs):
        block = re.search(rf"^{re.escape(name)}==.*?(?=^\S|\Z)", text, re.MULTILINE | re.DOTALL)
        assert block and "--hash=sha256:" in block.group(0), name
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    versions = {p["name"]: p.get("version") for p in lock["package"]}
    for name, ver in reqs:
        assert versions.get(name) == ver, (name, ver, versions.get(name))
    assert "pip install --require-hashes" in (IMG / "Dockerfile").read_text()


def test_requirements_in_has_runtime_deps_only() -> None:
    names = {re.split(r"[=<>\[]", x)[0] for x in (IMG / "requirements.in").read_text().split()}
    assert names == {"starlette", "uvicorn", "httpx"}
