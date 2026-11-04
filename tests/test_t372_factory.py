"""T-372 B3: one adapter factory for serve, the reaper and the CLI."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from llmhub.clock import FakeClock, SystemClock
from llmhub.configuration.loader import ConfigError
from llmhub.configuration.models import Config, PoolConfig, ProvidersConfig, RunpodConfig
from llmhub.provider_adapters.factory import aclose_adapters, build_provider_adapters
from llmhub.provider_adapters.mock import MockProvider
from llmhub.provider_adapters.runpod import RunpodAdapter
from runpod_fake import FAKE_KEY, IMAGE, FakeRunpod

KEY_ENV = "LLMHUB_RUNPOD_API_KEY_FILE"


def _cfg(tmp: Path, enabled: list[str], *, disk: int = 40) -> Config:
    pool = PoolConfig(
        name="cloud-27b", provider="runpod", gpu="RTX3090", vram_gb=24, pp_tps=1200, tg_tps=78,
        image=IMAGE, disk_gb=disk, max_price_usd_h=0.3, models=["m1"],
    )  # fmt: skip
    return Config.model_validate({
        "data_dir": tmp, "pools": {"cloud-27b": pool.model_dump()},
        "providers": ProvidersConfig(enabled=enabled, runpod=RunpodConfig()).model_dump(),
        "timeouts": {"create_visibility_grace_s": 600, "offer_wait_s": 300,
                     "provisioning_s": 900},
        "retry": {"create_unknown_max_retries": 0},
    })  # fmt: skip


def _key(tmp: Path) -> Path:
    p = tmp / "runpod.key"
    p.write_text(FAKE_KEY + "\n")
    os.chmod(p, 0o400)
    return p


def test_mock_only(tmp_path: Path) -> None:
    cfg = Config(data_dir=tmp_path, pools={})
    a = build_provider_adapters(cfg, {}, FakeClock())
    assert isinstance(a["mock"], MockProvider)


async def test_runpod_adapter_with_lazy_key(tmp_path: Path) -> None:
    clock = FakeClock()
    fake = FakeRunpod(clock)
    key = tmp_path / "runpod.key"  # does not exist yet: read only on the first call
    cfg = _cfg(tmp_path, ["runpod"], disk=55)
    a = build_provider_adapters(cfg, {KEY_ENV: str(key)}, clock, runpod_transport=fake.transport())
    rp = a["runpod"]
    assert isinstance(rp, RunpodAdapter) and rp.disk_gb_hint == 55 and rp.port == 8080
    assert fake.requests == []
    _key(tmp_path)
    assert await rp.list_owned() == []
    assert fake.requests[0].headers["authorization"] == f"Bearer {FAKE_KEY}"
    await aclose_adapters(a)
    assert rp.api._client is None


def test_runpod_needs_key_file_env(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, ["runpod"])
    with pytest.raises(ConfigError, match=KEY_ENV):
        build_provider_adapters(cfg, {}, FakeClock())
    with pytest.raises(ConfigError, match="plain") as ei:
        build_provider_adapters(cfg, {"LLMHUB_RUNPOD_API_KEY": "rpa_X1234"}, FakeClock())
    assert "rpa_X1234" not in str(ei.value)


def test_runpod_without_section_and_unknown_provider(tmp_path: Path) -> None:
    cfg = Config(data_dir=tmp_path, pools={})
    cfg.providers.enabled = ["runpod"]  # bypasses the validator (like a stale object)
    with pytest.raises(ConfigError, match="providers.runpod"):
        build_provider_adapters(cfg, {KEY_ENV: "/x"}, FakeClock())
    cfg.providers.enabled = ["vast"]
    with pytest.raises(ConfigError, match="vast"):
        build_provider_adapters(cfg, {}, FakeClock())


def test_server_and_reaper_use_the_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
                                           ) -> None:  # fmt: skip
    from llmhub import server
    from llmhub.reaper import __main__ as reaper_main

    cfg = _cfg(tmp_path, ["runpod"])
    env = {KEY_ENV: str(_key(tmp_path))}
    assert isinstance(server.build_adapters(cfg, env, FakeClock())["runpod"], RunpodAdapter)
    monkeypatch.setenv(KEY_ENV, env[KEY_ENV])
    got = reaper_main.build_adapters(cfg, SystemClock())
    assert isinstance(got["runpod"], RunpodAdapter)
