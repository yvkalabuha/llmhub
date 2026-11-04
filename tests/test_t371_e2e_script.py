"""T-371 F1: `scripts/e2e_mock.py` against a live stack (pre -> restart -> post-restart, kill)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from api_fixtures import ADMIN_KEY, GAME_KEY
from int_live import LiveStack
from llmhub.api_gateway.auth import hash_key
from llmhub.configuration.api_models import ClientConfig

pytestmark = pytest.mark.slow
ROOT = Path(__file__).resolve().parent.parent
E2E_KEY = "lh_test_e2e_key_00000000000000"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("e2e_mock", ROOT / "scripts/e2e_mock.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["e2e_mock"] = mod
    spec.loader.exec_module(mod)
    return mod


e2e = _load()


def _over() -> dict[str, Any]:
    return {"clients": {
        "game": ClientConfig(key_sha256=hash_key(GAME_KEY), allowed_models=["*"]),
        "e2e": ClientConfig(key_sha256=hash_key(E2E_KEY), allowed_models=["*"],
                            cloud_allowed=True, mock_only=True, max_active_jobs=4),
    }, "api": {"admin_key_sha256": hash_key(ADMIN_KEY), "docs": False}}  # fmt: skip


def _keys(tmp: Path) -> list[str]:
    k = tmp / "e2e.key"
    k.write_text(E2E_KEY + "\n")
    a = tmp / "admin.key"
    a.write_text(ADMIN_KEY + "\n")
    return ["--key-file", str(k), "--admin-key-file", str(a)]


def _run(phase: str, url: str, tmp: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    args = [phase, "--url", url, *_keys(tmp), "--state", str(tmp / "S.json"),
            "--cloud-model", "cloudonly", "--local-model", "localonly",
            "--long-tokens", "64", "--wait-s", "60", "--timeout", "60"]  # fmt: skip
    code = e2e.main(args)
    out, err = capsys.readouterr()
    assert E2E_KEY not in out + err and ADMIN_KEY not in out + err
    rep = json.loads(out)
    assert code == (0 if rep["ok"] else 1)
    return rep


def _failed(rep: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in rep["checks"] if not c["ok"]]


def test_e2e_phases_against_live_stack(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "data"
    data.mkdir()
    live = LiveStack(data, _over()).start()
    try:
        rep = _run("pre", live.url, tmp_path, capsys)
    finally:
        live.stop()
    names = {c["name"] for c in rep["checks"]}
    assert {"limit_429", "cloud_chat", "cloud_stream_done", "local_chat", "task_idempotent",
            "lease_create_renew_close", "restart_tasks_saved"} <= names  # fmt: skip
    assert _failed(rep) == [], rep
    assert len(json.loads((tmp_path / "S.json").read_text())["tasks"]) == 2
    # "docker restart": a new process on the same data dir
    live = LiveStack(data, _over()).start()
    try:
        rep = _run("post-restart", live.url, tmp_path, capsys)
        assert _failed(rep) == []
        rep = _run("kill", live.url, tmp_path, capsys)
        assert _failed(rep) == [], rep
    finally:
        live.stop()


def test_report_has_no_texts(tmp_path: Path) -> None:
    r = e2e.Report("pre")
    r.add("x", True, status=200)
    assert r.as_dict() == {"phase": "pre", "ok": True,
                           "checks": [{"name": "x", "ok": True, "status": 200}]}  # fmt: skip
    assert e2e.Report("pre").ok is False  # an empty report never passes


def test_bad_key_file_is_2(tmp_path: Path) -> None:
    assert e2e.main(["pre", "--key-file", str(tmp_path / "missing")]) == 2
