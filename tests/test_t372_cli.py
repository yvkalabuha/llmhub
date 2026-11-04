"""T-372 D3: provider-check, lifecycle-probe, scan-secrets, weights-token-check, config-check
with runpod (fake RunPod via the transport seam; no network, no real key)."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from harness import Harness
from llmhub import __main__ as cli
from llmhub import admin_cli
from llmhub.clock import FakeClock
from llmhub.cloud_manager.instance_env import derive_api_key
from llmhub.configuration.loader import load_config
from llmhub.configuration.models import PROBE_POOL
from llmhub.persistence.db import connect, migrate
from llmhub.provider_adapters.runpod import RunpodAdapter
from llmhub.tools.t372_ops import lifecycle_probe, scan_secrets, weights_token_for
from llmhub.weights.tokens import Verdict, check_request
from runpod_fake import FAKE_KEY, GPU_ID, IMAGE, FakeRunpod, runpod_cfg
from w_fixtures import INSTANCE_SECRET, WEIGHTS_SECRET, weights_cfg

LABEL_SECRET = "l" * 48
SMALL = "nvidia/cuda@sha256:" + "c" * 64


class AutoClock(FakeClock):
    """sleep_until jumps the clock (the probe polls without real waiting)."""

    async def sleep_until(self, t: float) -> None:
        if t > self.now():
            self.advance_to(t)


def _env(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k in list(os.environ):
        if k.startswith("LLMHUB_"):
            monkeypatch.delenv(k)
    key = tmp / "runpod.key"
    key.write_text(FAKE_KEY + "\n")
    key.chmod(0o400)
    monkeypatch.setenv("LLMHUB_RUNPOD_API_KEY_FILE", str(key))
    monkeypatch.setenv("LLMHUB_LABEL_SECRET", LABEL_SECRET)


def _config(tmp: Path, *, staleness: float = 0) -> Path:
    raw: dict[str, Any] = {
        "data_dir": str(tmp / "data"),
        "pools": {"cloud-27b": {
            "provider": "runpod", "gpu": "RTX3090", "vram_gb": 24, "pp_tps": 1200,
            "tg_tps": 78, "image": IMAGE, "disk_gb": 40, "max_price_usd_h": 0.30,
            "models": ["m1"],
        }},
        "providers": {"enabled": ["runpod"], "runpod": {"list_staleness_s": staleness}},
        "timeouts": {"create_visibility_grace_s": 600, "offer_wait_s": 300,
                     "provisioning_s": 960},
        "retry": {"create_unknown_max_retries": 0},
    }  # fmt: skip
    (tmp / "data").mkdir(exist_ok=True)
    migrate(connect(tmp / "data" / "state.db"))
    p = tmp / "config.yaml"
    p.write_text(yaml.safe_dump(raw))
    return p


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, Any, str]:
    code = cli.main(argv)
    out, err = capsys.readouterr()
    try:
        data = json.loads(out)
    except ValueError:
        data = out
    return code, data, err


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRunpod:
    f = FakeRunpod(FakeClock())
    monkeypatch.setattr(admin_cli, "RUNPOD_TRANSPORT", f.transport())
    return f


def test_config_check_runpod_needs_readable_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                 capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    _env(tmp_path, monkeypatch)
    p = _config(tmp_path)
    code, out, _ = _run(["config-check", "--config", str(p)], capsys)
    assert code == 0 and out.strip() == "ok"
    (tmp_path / "runpod.key").chmod(0o644)
    code, _, err = _run(["config-check", "--config", str(p)], capsys)
    assert code == 2 and FAKE_KEY not in err
    monkeypatch.delenv("LLMHUB_RUNPOD_API_KEY_FILE")
    code, _, err = _run(["config-check", "--config", str(p)], capsys)
    assert code == 2 and "LLMHUB_RUNPOD_API_KEY_FILE" in err


def test_provider_check_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                  capsys: pytest.CaptureFixture[str], fake: FakeRunpod) -> None:  # fmt: skip
    _env(tmp_path, monkeypatch)
    fake.volumes.append({"id": "v1", "name": "data"})
    code, out, err = _run(["provider-check", "--config", str(_config(tmp_path))], capsys)
    assert code == 0, err
    assert out["offers"][0]["offer_id"] == GPU_ID and out["owned"] == []
    assert out["balance_usd"] == 25.0 and out["billing_records"] == 0
    assert {r.method for r in fake.requests} <= {"GET", "POST"}
    assert all(r.url.path != "/v1/pods" or r.method == "GET" for r in fake.requests)
    assert FAKE_KEY not in json.dumps(out) + err


def test_provider_check_api_failure_exit_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str],
                                           fake: FakeRunpod) -> None:  # fmt: skip
    _env(tmp_path, monkeypatch)
    fake.inject("GET", "/pods", "status", status=401, body=FAKE_KEY)
    code, _, err = _run(["provider-check", "--config", str(_config(tmp_path))], capsys)
    assert code == 1 and "401" in err and FAKE_KEY not in err


def test_lifecycle_probe_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                             capsys: pytest.CaptureFixture[str], fake: FakeRunpod) -> None:  # fmt: skip
    _env(tmp_path, monkeypatch)
    p = _config(tmp_path)
    code, out, err = _run(["lifecycle-probe", "--config", str(p), "--image", SMALL,
                           "--poll-s", "0.01", "--max-s", "60"], capsys)  # fmt: skip
    assert code == 0, (out, err)
    assert out["zero"] and out["name_ok"] and out["destroyed"] and out["ref"]
    assert out["cost_per_hr"] == 0.225 and out["running_s"] is not None
    (body,) = fake.created_bodies
    assert body["dockerStartCmd"] == ["sleep", "600"] and body["imageName"] == SMALL
    assert fake.live_pods() == []
    conn = connect(tmp_path / "data" / "state.db")
    row = conn.execute("SELECT * FROM instances").fetchone()
    assert (row["pool"], row["state"], row["provider_ref"]) == (PROBE_POOL, "ERROR", out["ref"])
    assert row["last_error"] == "probe_done" and row["label"] == out["label"]


async def test_lifecycle_probe_measures_visibility_lag_and_staleness(tmp_path: Path) -> None:
    clock = AutoClock()
    fake = FakeRunpod(clock, visible_after_s=40, destroy_lag_s=25, boot_s=10)
    adapter = RunpodAdapter(fake.api(), runpod_cfg(list_staleness_s=30), clock, disk_gb_hint=20)
    conn = connect(tmp_path / "s.db")
    migrate(conn)
    cfg = load_config(_config(tmp_path))
    rep = await lifecycle_probe(conn, adapter, LABEL_SECRET.encode(), clock,
                                cfg, image=SMALL, gpu_type_id=GPU_ID,
                                poll_s=5, max_s=300)  # fmt: skip
    assert rep.zero and rep.visible_s is not None and rep.visible_s >= 40
    assert rep.destroy_gone_s is not None and rep.destroy_gone_s >= 25
    assert fake.live_pods() == []


async def test_lifecycle_probe_destroys_on_timeout(tmp_path: Path) -> None:
    clock = AutoClock()
    fake = FakeRunpod(clock, boot_s=10_000)  # never RUNNING
    adapter = RunpodAdapter(fake.api(), runpod_cfg(list_staleness_s=0), clock, disk_gb_hint=20)
    conn = connect(tmp_path / "s.db")
    migrate(conn)
    cfg = load_config(_config(tmp_path))
    rep = await lifecycle_probe(conn, adapter, b"x" * 32, clock, cfg, image=SMALL,
                                gpu_type_id=GPU_ID, max_s=60, poll_s=5)  # fmt: skip
    assert "max-s reached before RUNNING" in rep.notes and rep.zero and fake.live_pods() == []


async def test_controller_leaves_probe_rows_alone(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    t0 = h.clock.now()
    h.conn.execute(
        "INSERT INTO instances(id, op_id, pool, provider, label, state, state_since, created_at,"
        " max_lifetime_at, accrued_to, create_called_at, attempts)"
        " VALUES ('inst-p', '0123456789ab', ?, 'mock', 'llmhub-0123456789ab-00000000',"
        " 'PROVISIONING', ?, ?, ?, ?, ?, 1)",
        (PROBE_POOL, t0, t0, t0 + 900, t0, t0),
    )  # fmt: skip
    await h.run_until(t0 + 120)
    assert h.conn.execute("SELECT state FROM instances WHERE id='inst-p'").fetchone()[0] == (
        "PROVISIONING"
    )


def test_scan_secrets_counts_only(monkeypatch: pytest.MonkeyPatch,
                                  capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:  # fmt: skip
    _env(tmp_path, monkeypatch)
    inst = tmp_path / "instance"
    inst.write_text(INSTANCE_SECRET.decode() + "\n")
    inst.chmod(0o640)
    monkeypatch.setenv("LLMHUB_INSTANCE_SECRET_FILE", str(inst))
    op = "0123456789ab"
    key = derive_api_key(INSTANCE_SECRET, op)
    logs = f"boot ok\nusing key {key}\nauth {FAKE_KEY}\nv1.{op}.1793752200.{'A' * 43}\nready\n"
    monkeypatch.setattr("sys.stdin", io.StringIO(logs))
    code, out, _ = _run(["scan-secrets", "--op", op], capsys)
    assert code == 3 and out["matches"] >= 3 and out["api_key_checked"]
    printed = json.dumps(out)
    assert key not in printed and FAKE_KEY not in printed
    monkeypatch.setattr("sys.stdin", io.StringIO("clean log\n"))
    code, out, _ = _run(["scan-secrets", "--op", op], capsys)
    assert code == 0 and out["matches"] == 0


def test_scan_secrets_patterns() -> None:
    assert scan_secrets("-----BEGIN PRIVATE KEY-----", "0123456789ab", []) == 1
    assert scan_secrets("hf_" + "a" * 30 + " ghp_" + "b" * 30, "0123456789ab", []) == 2
    with pytest.raises(Exception, match="12 hex"):
        scan_secrets("", "nope", [])


def test_weights_token_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                             capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    for k in list(os.environ):
        if k.startswith("LLMHUB_"):
            monkeypatch.delenv(k)
    cfg = weights_cfg(tmp_path / "data")
    raw = cfg.model_dump(mode="json")
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw))
    conn = connect(tmp_path / "data" / "state.db")
    migrate(conn)
    op = "0123456789ab"
    now = 1_793_750_400.0
    conn.execute(
        "INSERT INTO instances(id, op_id, pool, provider, label, state, state_since, created_at,"
        " max_lifetime_at, accrued_to) VALUES ('i', ?, 'p1', 'mock', 'l', 'READY', ?, ?, ?, ?)",
        (op, now, now, now + 3600, now),
    )  # fmt: skip
    ws = tmp_path / "weights"
    ws.write_text(WEIGHTS_SECRET.decode() + "\n")
    ws.chmod(0o640)
    monkeypatch.setenv("LLMHUB_WEIGHTS_SECRET_FILE", str(ws))
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(403)

    monkeypatch.setattr(admin_cli, "WEIGHTS_TRANSPORT", httpx.MockTransport(handler))
    code, out, err = _run(["weights-token-check", "--config", str(p), "--op", op], capsys)
    assert code == 0, err and out == {"op": op, "http_status": 403}
    (req,) = seen
    assert str(req.url) == f"https://203.0.113.7:9443/w/{op}/q/model.gguf"
    token = req.headers["authorization"].removeprefix("Bearer ")
    assert token not in json.dumps(out) + err
    verdict, _ = check_request(WEIGHTS_SECRET, f"Bearer {token}", f"/w/{op}/q/model.gguf",
                               now + 60, lambda _o: "LOADING_MODEL",
                               frozenset({"q/model.gguf"}))  # fmt: skip
    assert verdict == Verdict.OK  # the same token the pod got (valid while loading)
    assert weights_token_for(conn, cfg, WEIGHTS_SECRET, op) == token
