"""T-371 D1 (F1): the llama-server key is read lazily from `api_key_file`, re-read after 401."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from fake_llama import FakeLlama
from llmhub.clock import FakeClock
from llmhub.configuration.api_models import LocalBackendConfig
from llmhub.configuration.loader import load_config
from llmhub.local_inference.client import (
    KEY_RELOAD_MIN_S,
    LocalBackendClient,
    LocalError,
    file_key_source,
)
from llmhub.server import build_service

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "config_api.yaml"
ENV = {"LLMHUB_LABEL_SECRET": "l" * 40}
REQ = {"messages": [{"role": "user", "content": "hi"}]}
KEY1 = "llama-key-one-0000000000"
KEY2 = "llama-key-two-1111111111"


def _key(tmp: Path, text: str) -> Path:
    p = tmp / "api-key"
    p.write_text(text + "\n")
    p.chmod(0o640)
    return p


def _cfg_with_file(path: Path) -> LocalBackendConfig:
    return LocalBackendConfig(
        url="http://fake-llama/v1", model="m", ctx_tokens=8192, pp_tps=100, tg_tps=5,
        api_key_file=path,
    )  # fmt: skip


def test_build_service_does_not_read_key_file(tmp_path: Path) -> None:
    cfg = load_config(FIXTURE)
    cfg.data_dir = tmp_path / "data"
    missing = tmp_path / "missing-key"
    cfg.local_backends["cpu"].api_key_file = missing
    svc = build_service(cfg, ENV, FakeClock())
    try:
        assert svc.backends["cpu"]._key_source is not None
        assert svc.backends["cpu"]._api_key is None  # nothing read yet
    finally:
        svc.rt.conn.close()


async def test_first_chat_reads_file_and_sends_bearer(tmp_path: Path) -> None:
    path = _key(tmp_path, KEY1)
    fake = FakeLlama(api_key=KEY1)
    c = LocalBackendClient("cpu", _cfg_with_file(path), None, http=fake.client(),
                           clock=FakeClock(), key_source=file_key_source(path))  # fmt: skip
    r = await c.chat(REQ, timeout_s=10)
    assert r.tokens_out > 0
    assert fake.chat_requests[0].headers["authorization"] == f"Bearer {KEY1}"
    assert KEY1 not in repr(c)


async def test_401_rereads_rotated_key_and_retries_once(tmp_path: Path) -> None:
    path = _key(tmp_path, KEY1)
    fake = FakeLlama(api_key=KEY1)
    clock = FakeClock()
    c = LocalBackendClient("cpu", _cfg_with_file(path), None, http=fake.client(), clock=clock,
                           key_source=file_key_source(path))  # fmt: skip
    await c.chat(REQ, timeout_s=10)
    fake.api_key = KEY2  # llama key rotated
    _key(tmp_path, KEY2)
    clock.advance_to(clock.now() + KEY_RELOAD_MIN_S)
    r = await c.chat(REQ, timeout_s=10)
    assert r.tokens_out > 0
    auths = [s.headers.get("authorization") for s in fake.chat_requests]
    assert auths == [f"Bearer {KEY1}", f"Bearer {KEY1}", f"Bearer {KEY2}"]


async def test_401_reread_is_rate_limited(tmp_path: Path) -> None:
    path = _key(tmp_path, KEY1)
    fake = FakeLlama(api_key="something-else-entirely")
    clock = FakeClock()
    c = LocalBackendClient("cpu", _cfg_with_file(path), None, http=fake.client(), clock=clock,
                           key_source=file_key_source(path))  # fmt: skip
    with pytest.raises(LocalError) as ei:
        await c.chat(REQ, timeout_s=10)
    assert ei.value.code == "local_unavailable"
    n = len(fake.chat_requests)
    assert n == 1  # first read just happened: no second read within 30 s, no retry
    with pytest.raises(LocalError):
        await c.chat(REQ, timeout_s=10)
    assert len(fake.chat_requests) == 2
    clock.advance_to(clock.now() + KEY_RELOAD_MIN_S)
    with pytest.raises(LocalError):
        await c.chat(REQ, timeout_s=10)
    assert len(fake.chat_requests) == 4  # one re-read, one retry, still 401


async def test_missing_file_is_local_unavailable_without_content(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "nope"
    fake = FakeLlama()
    c = LocalBackendClient("cpu", _cfg_with_file(path), None, http=fake.client(),
                           clock=FakeClock(), key_source=file_key_source(path))  # fmt: skip
    with caplog.at_level(logging.DEBUG), pytest.raises(LocalError) as ei:
        await c.chat(REQ, timeout_s=10)
    assert ei.value.code == "local_unavailable"
    assert fake.chat_requests == []
    # a file that appears later is picked up after the re-read interval
    path.write_text(KEY1 + "\n")
    path.chmod(0o640)
    c._clock.advance_to(c._clock.now() + KEY_RELOAD_MIN_S)  # type: ignore[attr-defined]
    await c.chat(REQ, timeout_s=10)
    assert KEY1 not in caplog.text and KEY1 not in str(ei.value)


async def test_probe_works_without_key_file(tmp_path: Path) -> None:
    path = tmp_path / "nope"
    fake = FakeLlama()
    cfg = _cfg_with_file(path).model_copy(update={"probe": "health"})
    c = LocalBackendClient("cpu", cfg, None, http=fake.client(), clock=FakeClock(),
                           key_source=file_key_source(path))  # fmt: skip
    assert (await c.probe()).ok is True
    assert "authorization" not in fake.requests[0].headers


def test_key_file_content_never_in_errors(tmp_path: Path) -> None:
    p = _key(tmp_path, KEY1)
    p.chmod(0o644)  # others may read: refused
    src = file_key_source(p)
    from llmhub.local_inference.client import KeyUnavailable

    with pytest.raises(KeyUnavailable) as ei:
        src()
    assert KEY1 not in str(ei.value)


def test_reaper_once_starts_without_key_mount(tmp_path: Path) -> None:
    raw = yaml.safe_load(FIXTURE.read_text())
    raw["data_dir"] = str(tmp_path / "data")
    for b in raw.get("local_backends", {}).values():
        b.pop("api_key_env", None)
        b["api_key_file"] = str(tmp_path / "not-mounted" / "api-key")
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text(yaml.safe_dump(raw))
    env = {k: v for k, v in os.environ.items() if not k.startswith("LLMHUB_")}
    env.update(ENV)
    r = subprocess.run(
        [sys.executable, "-m", "llmhub.reaper", "--config", str(cfgp), "--once"],
        env=env, capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
