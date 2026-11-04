"""T-369 O2/O3: every example runs against the live app (uvicorn, Mock cloud, fake llama).

curl scripts and the Python examples always run (slow); the C# example needs `dotnet`
(skipped without it: gameDev runs it on their machine), its paths and headers are checked
against the OpenAPI document always.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from int_live import LiveStack

ROOT = Path(__file__).resolve().parent.parent
CURL = sorted((ROOT / "examples" / "curl").glob("*.sh"))
PYTHON = sorted((ROOT / "examples" / "python").glob("*.py"))
CSHARP = ROOT / "examples" / "csharp" / "LlmHubExample"
GUIDE = ROOT / "docs" / "integration.md"


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LiveStack]:
    stack = LiveStack(tmp_path_factory.mktemp("live")).start()
    yield stack
    stack.stop()


def test_every_curl_script_is_listed_in_the_guide() -> None:
    assert len(CURL) == 9
    text = GUIDE.read_text(encoding="utf-8")
    listed = set(re.findall(r"examples/curl/(\d\d_[a-z_]+\.sh)", text))
    assert listed == {p.name for p in CURL}
    for p in PYTHON:
        assert f"examples/python/{p.name}" in text
    assert "examples/csharp/LlmHubExample" in text


@pytest.mark.slow
@pytest.mark.parametrize("script", CURL, ids=[p.name for p in CURL])
def test_curl_example(live: LiveStack, script: Path) -> None:
    out = subprocess.run(
        ["bash", str(script)], env=live.env(), capture_output=True, text=True, timeout=120,
        check=False,
    )  # fmt: skip
    assert out.returncode == 0, f"{script.name}\nstdout:\n{out.stdout}\nstderr:\n{out.stderr}"


@pytest.mark.slow
@pytest.mark.parametrize("example", PYTHON, ids=[p.name for p in PYTHON])
def test_python_example(live: LiveStack, example: Path) -> None:
    out = subprocess.run(
        [sys.executable, str(example)], env=live.env(), capture_output=True, text=True,
        timeout=120, check=False,
    )  # fmt: skip
    assert out.returncode == 0, f"{example.name}\n{out.stdout}\n{out.stderr}"
    assert "answer:" in out.stdout


def test_csharp_paths_and_headers_are_in_the_openapi() -> None:
    spec = yaml.safe_load((ROOT / "docs" / "openapi.yaml").read_text(encoding="utf-8"))
    source = (CSHARP / "Program.cs").read_text(encoding="utf-8")
    paths = set(re.findall(r'"(/v1/[^"]*)"', source)) | {
        re.sub(r"\{[^}]+\}", "{x}", p) for p in re.findall(r'\$"(/v1/[^"]*)"', source)
    }
    templates = {re.sub(r"\{[^}]+\}", "{x}", p) for p in spec["paths"]}
    assert paths and {re.sub(r"\{[^}]+\}", "{x}", p) for p in paths} <= templates
    documented = {"Idempotency-Key", *(n for n in re.findall(r"X-LLMHub-[A-Za-z-]+",
                                                            yaml.safe_dump(spec)))}  # fmt: skip
    used = set(re.findall(r'"(Idempotency-Key|X-LLMHub-[A-Za-z-]+)"', source))
    assert used and used <= documented
    assert "TimeSpan.FromMinutes(10)" in source  # client timeout above the 480 s window


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("dotnet") is None, reason="dotnet SDK not installed")
def test_csharp_example_builds_and_runs(live: LiveStack, tmp_path: Path) -> None:
    work = tmp_path / "LlmHubExample"
    shutil.copytree(CSHARP, work)
    env = {**live.env(), "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1"}
    build = subprocess.run(["dotnet", "build", "-nologo"], cwd=work, env=env,
                           capture_output=True, text=True, timeout=600, check=False)  # fmt: skip
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run(["dotnet", "run", "--no-build"], cwd=work, env=env,
                         capture_output=True, text=True, timeout=300, check=False)  # fmt: skip
    assert run.returncode == 0, run.stdout + run.stderr
    assert "answer:" in run.stdout
