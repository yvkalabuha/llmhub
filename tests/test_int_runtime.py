"""T-369 I1: composition root (`build_service`), CLI, speed EWMA consumer, shutdown."""

from __future__ import annotations

import logging
import subprocess
import sys
import types
from pathlib import Path

import pytest

from harness import TEST_SECRET
from int_fixtures import ENV
from llmhub.api_gateway.auth import hash_key
from llmhub.budget_manager.estimate import get_speeds
from llmhub.clock import FakeClock
from llmhub.cloud_manager.http_inference import HttpInferenceBackend, Timings
from llmhub.configuration.loader import ConfigError, load_config
from llmhub.configuration.models import Config
from llmhub.cost_tracker.txutil import state_get
from llmhub.local_inference.worker import LocalWorker
from llmhub.provider_adapters.mock_inference import MockInference
from llmhub.server import build_service, make_speed_consumer, mtp_key

FIXTURE = Path(__file__).parent / "fixtures" / "config_api.yaml"


def _cfg(tmp: Path) -> Config:
    cfg = load_config(FIXTURE)
    cfg.data_dir = tmp
    return cfg


def test_build_service_from_fixture(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    cfg = _cfg(tmp_path)
    cfg.local_backends["cpu"].api_key_env = "LLMHUB_TEST_LLAMA_KEY"
    with caplog.at_level(logging.WARNING, logger="llmhub.server"):
        svc = build_service(cfg, ENV, FakeClock())
    rt = svc.rt
    # one connection shared by everybody, the worker shares the controller's boot id
    assert rt.controller.conn is rt.conn and rt.waiter.conn is rt.conn
    assert isinstance(rt.worker, LocalWorker)
    assert rt.worker.conn is rt.conn and rt.worker.boot_id == rt.controller.boot_id
    assert isinstance(rt.controller.inference["mock"], MockInference)
    assert svc.http_inference is None and not svc.safe_mode
    clients = rt.conn.execute("SELECT id, key_hash, disabled_at FROM clients").fetchall()
    assert [(r["id"], r["disabled_at"]) for r in clients] == [("game", None)]
    assert rt.registry.by_key("nope") is None
    # missing key variable: warning naming the variable (never a value), backend keyless
    assert "LLMHUB_TEST_LLAMA_KEY" in caplog.text
    assert svc.backends["cpu"]._headers() == {}
    assert (tmp_path / "state.db").exists()
    rt.conn.close()


def test_local_backend_key_from_env_only_in_header(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.local_backends["cpu"].api_key_env = "LLMHUB_TEST_LLAMA_KEY"
    svc = build_service(cfg, {**ENV, "LLMHUB_TEST_LLAMA_KEY": "llama-key-123"}, FakeClock())
    b = svc.backends["cpu"]
    assert b._headers() == {"Authorization": "Bearer llama-key-123"}
    assert "llama-key-123" not in repr(b)
    svc.rt.conn.close()


def test_label_secret_required(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="LLMHUB_LABEL_SECRET"):
        build_service(_cfg(tmp_path), {}, FakeClock())


def test_unknown_provider_has_no_adapter(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.providers.enabled = ["mock", "runpod"]
    with pytest.raises(ConfigError, match="runpod"):
        build_service(cfg, ENV, FakeClock())


def test_pool_with_weights_needs_instance_secrets(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.pools["p1"].weights = "w"  # validated configs only get here with a weight set
    with pytest.raises(ConfigError, match="LLMHUB_INSTANCE_SECRET"):
        build_service(cfg, ENV, FakeClock())


async def test_real_provider_gets_http_backend_and_is_closed(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    fake_real = types.SimpleNamespace(name="realcloud", capabilities=0)
    env = {
        **ENV,
        "LLMHUB_INSTANCE_SECRET": TEST_SECRET.decode(),
        "LLMHUB_WEIGHTS_SECRET": "w" * 40,  # T-371: must differ from the instance secret
    }
    svc = build_service(cfg, env, FakeClock(), adapters={"realcloud": fake_real})  # type: ignore[dict-item]
    http = svc.http_inference
    assert isinstance(http, HttpInferenceBackend)
    assert svc.rt.controller.inference["realcloud"] is http
    assert http.on_timings is not None and http.on_pin_mismatch is not None
    closed: list[bool] = []

    async def fake_aclose() -> None:
        closed.append(True)

    http.aclose = fake_aclose  # type: ignore[method-assign]
    await svc.aclose()
    assert closed == [True]
    svc.rt.conn.close()


def test_speed_consumer_feeds_pool_ewma_and_mtp(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    clock = FakeClock()
    svc = build_service(cfg, ENV, clock)
    conn = svc.rt.conn
    conn.execute("INSERT INTO clients(id, name, key_hash, created_at) VALUES ('c','c','x',0)")
    for jid, pool in (("j1", "p1"), ("j2", None)):
        conn.execute(
            "INSERT INTO jobs(id, client_id, body_sha256, priority, prio_rank, model_alias,"
            " policy, data_class, pool, status, route, created_at, est_tokens_out, req_path)"
            " VALUES (?, 'c', 'x', 'normal', 1, 'big', 'BALANCED', 'cloud_allowed', ?,"
            " 'running', 'cloud', 0, 10, '')",
            (jid, pool),
        )
    consume = make_speed_consumer(conn, cfg, clock)
    assert get_speeds(conn, "p1", 1200, 78) == (1200, 78)
    consume("j1", Timings(2000, 1000.0, 100, 1000.0, 60, 45))  # pp 2000, tg 100, accept 0.75
    pp, tg = get_speeds(conn, "p1", 1200, 78)
    assert pp == pytest.approx(0.3 * 2000 + 0.7 * 1200)
    assert tg == pytest.approx(0.3 * 100 + 0.7 * 78)
    assert float(state_get(conn, mtp_key("p1")) or 0) == pytest.approx(0.75)
    consume("j1", Timings(2000, 1000.0, 100, 1000.0, 10, 5))  # accept 0.5
    assert float(state_get(conn, mtp_key("p1")) or 0) == pytest.approx(0.3 * 0.5 + 0.7 * 0.75)
    pp, tg = get_speeds(conn, "p1", 1200, 78)
    consume("j2", Timings(1, 1.0, 1, 1.0))  # local job: no pool, ignored
    consume("missing", Timings(1, 1.0, 1, 1.0))
    consume("j1", Timings(0, 0.0, 0, 0.0))  # no speeds: nothing changes
    assert get_speeds(conn, "p1", 1200, 78) == (pp, tg)
    conn.close()


def test_cli_keys_new_prints_key_and_hash() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "llmhub", "keys", "new", "--name", "gamedev"],
        capture_output=True, text=True, check=True, timeout=60,
    )  # fmt: skip
    lines = dict(
        line.split(": ", 1) for line in out.stdout.splitlines() if not line.startswith("#")
    )
    key, digest = lines["key"], lines["key_sha256"]
    assert key.startswith("lh_") and len(key) > 40
    assert digest == hash_key(key)
    assert f'clients.gamedev.key_sha256: "{digest}"' in out.stdout
    assert key not in out.stderr


def test_cli_serve_bad_config_exit_2(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("pools: 5\n")
    out = subprocess.run(
        [sys.executable, "-m", "llmhub", "serve", "--config", str(bad)],
        check=False, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert out.returncode == 2 and "config error" in out.stderr
