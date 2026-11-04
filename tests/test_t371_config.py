"""T-371 C1: config validators for the deploy (host, mock_only, probes, llama key file, docs)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from llmhub.configuration.api_models import ApiConfig, LocalBackendConfig, LocalConfig
from llmhub.configuration.loader import ConfigError, load_config
from llmhub.configuration.weights_models import WeightsConfig

H = "a" * 64
BACKEND = {
    "url": "http://llama.invalid:18091/v1", "model": "m", "ctx_tokens": 4096, "pp_tps": 10,
    "tg_tps": 2,
}  # fmt: skip


def _write(tmp: Path, **over: Any) -> Path:
    raw: dict[str, Any] = {"data_dir": str(tmp / "data"), "pools": {}}
    raw.update(over)
    p = tmp / "config.yaml"
    p.write_text(yaml.safe_dump(raw))
    return p


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "[::]", "", " ", "*"])
def test_wildcard_api_host_rejected(host: str) -> None:
    with pytest.raises(ValueError):
        ApiConfig(host=host)


@pytest.mark.parametrize("host", ["10.8.0.1", "127.0.0.1", "::1"])
def test_specific_api_host_accepted(host: str) -> None:
    assert ApiConfig(host=host).host == host


def test_docs_flag_default_true() -> None:
    assert ApiConfig().docs is True
    assert ApiConfig(docs=False).docs is False


def test_mock_only_client_requires_mock_providers(tmp_path: Path) -> None:
    client = {"key_sha256": H, "allowed_models": [], "cloud_allowed": True, "mock_only": True}
    ok = _write(tmp_path, clients={"e2e": client})
    assert load_config(ok).clients["e2e"].mock_only is True
    bad = _write(tmp_path, clients={"e2e": client}, providers={"enabled": ["mock", "runpod"]})
    with pytest.raises(ConfigError, match="mock_only"):
        load_config(bad)
    none = _write(tmp_path, clients={"e2e": client}, providers={"enabled": []})
    with pytest.raises(ConfigError, match="mock_only"):
        load_config(none)


def test_mock_only_false_allows_any_provider_list(tmp_path: Path) -> None:
    client = {"key_sha256": H, "allowed_models": []}
    # T-372: runpod needs its section and the spend guards (tests/test_t372_config.py)
    p = _write(tmp_path, clients={"c": client},
               providers={"enabled": ["mock", "runpod"], "runpod": {}},
               timeouts={"create_visibility_grace_s": 600, "offer_wait_s": 300,
                         "provisioning_s": 900},
               retry={"create_unknown_max_retries": 0})  # fmt: skip
    assert load_config(p).clients["c"].mock_only is False


def test_max_active_jobs_default_and_bounds() -> None:
    from llmhub.configuration.api_models import ClientConfig

    assert ClientConfig(key_sha256=H, allowed_models=[]).max_active_jobs == 32
    with pytest.raises(ValueError):
        ClientConfig(key_sha256=H, allowed_models=[], max_active_jobs=0)


def test_probe_interval_lower_bound() -> None:
    assert LocalConfig().probe_interval_s == 30
    assert LocalConfig(probe_interval_s=10).probe_interval_s == 10
    with pytest.raises(ValueError):
        LocalConfig(probe_interval_s=9.9)


@pytest.mark.parametrize("mode", ["slots", "health", "models"])
def test_probe_modes(mode: str) -> None:
    assert LocalBackendConfig.model_validate({**BACKEND, "probe": mode}).probe == mode


def test_probe_mode_unknown_rejected() -> None:
    with pytest.raises(ValueError):
        LocalBackendConfig.model_validate({**BACKEND, "probe": "completion"})


def test_key_env_and_key_file_are_exclusive() -> None:
    with pytest.raises(ValueError, match="api_key_env"):
        LocalBackendConfig(**BACKEND, api_key_env="LLAMA_KEY", api_key_file=Path("/run/k"))


def test_load_config_never_opens_api_key_file(tmp_path: Path) -> None:
    missing = tmp_path / "no" / "such" / "key"
    p = _write(tmp_path, local_backends={"cpu": {**BACKEND, "api_key_file": str(missing)}})
    cfg = load_config(p)
    assert cfg.local_backends["cpu"].api_key_file == missing
    assert not missing.exists()


def test_min_mem_available_default() -> None:
    assert WeightsConfig().min_mem_available_mib == 8192
    assert WeightsConfig(min_mem_available_mib=0).min_mem_available_mib == 0
    with pytest.raises(ValueError):
        WeightsConfig(min_mem_available_mib=-1)
