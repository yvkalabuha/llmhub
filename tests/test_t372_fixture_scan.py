"""T-372 F2/F3: RunPod fixtures carry no secrets/e-mails/unlisted fields; guard units are
static-checked (paths only, `watch --once --auto-destroy`, enable/disable in the runbook)."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

from t372_scripts import load

ROOT = Path(__file__).resolve().parent.parent
scan = load("image_secret_scan")
guard = load("runpod_guard")

FIX = ROOT / "tests" / "fixtures" / "runpod"
EMAIL = re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
WS = ROOT / "deploy" / "workstation"
FILES = sorted(p for p in FIX.rglob("*") if p.is_file())


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(FIX)))
def test_fixture_has_no_secret_or_email(path: Path) -> None:
    data = path.read_bytes()
    assert scan.scan_stream(io.BytesIO(data)) == set(), path.name
    if path.name != "openapi.json":  # the public schema has example addresses
        assert not EMAIL.search(data), path.name


@pytest.mark.parametrize("path", sorted((FIX / "recorded").glob("*.json")),
                         ids=lambda p: p.name)  # fmt: skip
def test_recorded_only_allowlisted_fields(path: Path) -> None:
    doc = json.loads(path.read_text())
    assert doc["_source"] == "recorded"
    allowed = set(guard.ALLOW[doc["_kind"]])
    for item in doc["items"]:
        assert set(item) <= allowed, sorted(set(item) - allowed)


def test_synthetic_fixtures_are_marked() -> None:
    for p in (FIX / "synthetic").glob("*.json"):
        assert json.loads(p.read_text())["_source"] == "synthetic", p.name


def test_guard_units() -> None:
    svc = (WS / "llmhub-runpod-guard.service").read_text()
    (exec_line,) = [x for x in svc.splitlines() if x.startswith("ExecStart=")]
    assert "watch --once --auto-destroy" in exec_line and "python3 -I" in exec_line
    assert "Type=oneshot" in svc and "EnvironmentFile=%h/.config/llmhub-guard/guard.env" in svc
    timer = (WS / "llmhub-runpod-guard.timer").read_text()
    assert "OnBootSec=60" in timer and "OnUnitActiveSec=60" in timer


def test_guard_env_has_paths_only() -> None:
    """Paths, plus the expiry time GUARD_UNTIL (REV-S9); never a key value."""
    for line in (WS / "guard.env.example").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.partition("=")
        if name == "GUARD_UNTIL":
            assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value), line
            continue
        assert name.startswith("GUARD_") and value.startswith("/"), line
        assert not re.search(r"rpa_|ghp_|hf_|sk-", value)


def test_runbook_enables_and_removes_the_timer() -> None:
    doc = (ROOT / "docs" / "t372-live-test.md").read_text()
    assert "systemctl --user enable --now llmhub-runpod-guard.timer" in doc
    assert "systemctl --user disable --now llmhub-runpod-guard.timer" in doc
    l0, l8 = doc.index("### L0"), doc.index("### L8")
    assert l0 < doc.index("enable --now llmhub-runpod-guard.timer") < doc.index("### L1")
    assert doc.index("disable --now llmhub-runpod-guard.timer") > l8
    assert "rm -f ~/.config/systemd/user/llmhub-runpod-guard" in doc
