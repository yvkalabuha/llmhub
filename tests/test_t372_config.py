"""T-372 B1/B2: RunPod config section, spend guards as validators, lazy provider key file."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from llmhub.configuration.loader import ConfigError, load_config, load_provider_key
from llmhub.configuration.models import RetryConfig, RunpodConfig
from llmhub.configuration.weights_models import WeightsConfig

DIGEST = "ghcr.io/yvkalabuha/llmhub-infer@sha256:" + "a" * 64
POOL: dict[str, Any] = {
    "provider": "runpod", "gpu": "RTX3090", "vram_gb": 24, "pp_tps": 1200, "tg_tps": 78,
    "image": DIGEST, "disk_gb": 40, "max_price_usd_h": 0.30, "models": ["m1"],
}  # fmt: skip
RAW: dict[str, Any] = {
    "pools": {"cloud-27b": POOL},
    "providers": {"enabled": ["runpod"], "runpod": {}},
    "timeouts": {"create_visibility_grace_s": 600, "offer_wait_s": 300, "provisioning_s": 960},
    "retry": {"create_unknown_max_retries": 0},
}


def _write(tmp: Path, raw: dict[str, Any]) -> Path:
    data = {"data_dir": str(tmp / "data"), **raw}
    p = tmp / "config.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def _raw(**patch: Any) -> dict[str, Any]:
    raw = copy.deepcopy(RAW)
    for dotted, value in patch.items():
        node = raw
        *head, last = dotted.split("__")
        for k in head:
            node = node.setdefault(k, {})
        if value is _DEL:
            node.pop(last, None)
        else:
            node[last] = value
    return raw


_DEL = object()


def test_valid_runpod_config(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, RAW))
    rp = cfg.providers.runpod
    assert rp is not None and rp.cloud_type == "COMMUNITY"
    assert rp.gpu_type_ids == {"RTX3090": "NVIDIA GeForce RTX 3090"}
    assert rp.allowed_cuda_versions == ["12.8", "12.9"]
    assert (rp.price_ceiling_usd_h, rp.max_live_pods, rp.list_staleness_s) == (0.50, 1, 30)
    assert rp.call_timeout_s == 20 and rp.container_disk_usd_gb_month == 0.10


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        ({"providers__runpod": _DEL}, "providers.runpod"),
        ({"timeouts__create_visibility_grace_s": 599}, "create_visibility_grace_s"),
        ({"retry__create_unknown_max_retries": 1}, "create_unknown_max_retries"),
        ({"pools__cloud-27b__image": "ghcr.io/x/llmhub-infer:0.3.0"}, "sha256"),
        ({"pools__cloud-27b__image": "x@sha256:" + "A" * 64}, "sha256"),
        ({"pools__cloud-27b__infer": {"transport": "direct"}}, "transport"),
        ({"pools__cloud-27b__max_price_usd_h": 0.51}, "price_ceiling"),
        ({"pools__cloud-27b__gpu": "H100"}, "gpu_type_ids"),
        ({"pools__cloud-27b__disk_gb": 0}, "disk_gb"),
        ({"providers__enabled": ["runpod", "vast"]}, "unknown provider"),
        ({"providers__runpod__price_ceiling_usd_h": 0.6}, "price_ceiling_usd_h"),
        ({"providers__runpod__max_live_pods": 5}, "max_live_pods"),
        ({"providers__runpod__cloud_type": "SECURE"}, "cloud_type"),
        ({"retry__host_blacklist_s": 3599}, "host_blacklist_s"),
        ({"timeouts__provisioning_s": 899}, "provisioning_s"),
        ({"weights": {"token_ttl_max_s": 1801}}, "token_ttl_max_s"),
    ],
)
def test_guard_violations_rejected(tmp_path: Path, patch: dict[str, Any], match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_config(_write(tmp_path, _raw(**patch)))


def test_reserved_pool_name(tmp_path: Path) -> None:
    raw = _raw()
    raw["pools"] = {"_probe": raw["pools"]["cloud-27b"]}
    with pytest.raises(ConfigError, match="reserved"):
        load_config(_write(tmp_path, raw))


def test_pool_price_ceiling_from_section(tmp_path: Path) -> None:
    raw = _raw(providers__runpod__price_ceiling_usd_h=0.25)
    with pytest.raises(ConfigError, match="price_ceiling"):
        load_config(_write(tmp_path, raw))


def test_mock_pools_unaffected_when_runpod_enabled(tmp_path: Path) -> None:
    raw = _raw()
    raw["providers"]["enabled"] = ["mock", "runpod"]
    raw["pools"]["m"] = {**POOL, "provider": "mock", "image": "llmhub-infer:mock",
                         "gpu": "RTX4090"}  # fmt: skip
    assert load_config(_write(tmp_path, raw)).pools["m"].image == "llmhub-infer:mock"


def test_defaults_keep_mock_behaviour() -> None:
    r = RetryConfig()
    assert r.host_blacklist_s == 86400 and r.create_unknown_max_retries == 1
    assert WeightsConfig().token_ttl_max_s == 1800
    with pytest.raises(ValidationError):
        RetryConfig(create_unknown_max_retries=4)
    with pytest.raises(ValidationError):
        WeightsConfig(token_ttl_max_s=299)
    assert RunpodConfig().max_live_pods == 1


def test_runpod_section_without_enable_is_allowed(tmp_path: Path) -> None:
    raw = _raw(providers__enabled=["mock"])
    raw["pools"]["cloud-27b"]["provider"] = "mock"
    raw["retry"]["create_unknown_max_retries"] = 1
    assert load_config(_write(tmp_path, raw)).providers.runpod is not None


# ---------------------------------------------------------------- B2: provider key file


def _key(tmp: Path, value: str = "rpa_TESTKEY0123456789", mode: int = 0o400) -> Path:
    p = tmp / "runpod"
    p.write_text(value + "\n")
    os.chmod(p, mode)
    return p


def test_provider_key_lazy_and_file_only(tmp_path: Path) -> None:
    p = _key(tmp_path)
    env = {"LLMHUB_RUNPOD_API_KEY_FILE": str(p)}
    get = load_provider_key("runpod", env)
    os.chmod(p, 0o600)
    p.write_text("rpa_ROTATED000000000\n")  # read on use, not at load time
    assert get() == "rpa_ROTATED000000000"


def test_provider_key_plain_env_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="_FILE") as ei:
        load_provider_key("runpod", {"LLMHUB_RUNPOD_API_KEY": "rpa_PLAINVALUE123"})
    assert "rpa_PLAINVALUE123" not in str(ei.value)


def test_provider_key_missing(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="LLMHUB_RUNPOD_API_KEY_FILE"):
        load_provider_key("runpod", {})


def test_provider_key_other_bits_refused_without_value(tmp_path: Path) -> None:
    p = _key(tmp_path, "rpa_SECRETVALUE0001", 0o644)
    get = load_provider_key("runpod", {"LLMHUB_RUNPOD_API_KEY_FILE": str(p)})
    with pytest.raises(ConfigError) as ei:
        get()
    assert "rpa_SECRETVALUE0001" not in str(ei.value)


def test_provider_key_empty_refused(tmp_path: Path) -> None:
    p = _key(tmp_path, "")
    get = load_provider_key("runpod", {"LLMHUB_RUNPOD_API_KEY_FILE": str(p)})
    with pytest.raises(ConfigError, match="empty"):
        get()
