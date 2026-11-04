"""T-369 G1: API/router/local configuration models."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from llmhub.configuration.loader import load_config
from llmhub.configuration.models import Config
from llmhub.task_queue.models import Priority

H1 = "1" * 64
H2 = "2" * 64
FIX = Path(__file__).parent / "fixtures" / "config_api.yaml"

POOL: dict[str, Any] = {
    "name": "p1", "provider": "mock", "gpu": "g", "vram_gb": 24, "pp_tps": 1, "tg_tps": 1,
    "image": "i", "disk_gb": 1, "models": ["a"],
}  # fmt: skip
BACKEND = {"url": "http://127.0.0.1:1/v1", "model": "m", "ctx_tokens": 2048, "pp_tps": 1,
           "tg_tps": 1}  # fmt: skip


def cfg(**extra: Any) -> Config:
    base: dict[str, Any] = {
        "data_dir": "/tmp/x", "pools": {"p1": POOL},
        "local_backends": {"cpu": BACKEND},
        "models": {"a": {"local": "cpu"}},
    }  # fmt: skip
    return Config.model_validate(base | extra)


def test_defaults() -> None:
    c = Config(data_dir=Path("/tmp/x"), pools={})
    assert c.local_backends == {} and c.models == {} and c.task_types == {} and c.clients == {}
    assert c.router.default_policy == "BALANCED" and c.router.bytes_per_token == 3.5
    assert c.local.concurrency == 1 and c.local.max_attempts == 2
    assert c.api.port == 8095 and c.api.sync_wait_s == 480 and c.api.admin_key_sha256 is None
    assert c.api.retry_after_max_s == 900 and c.api.max_body_bytes == 4_000_000


def test_defaults_not_shared() -> None:
    a = Config(data_dir=Path("/tmp/x"), pools={})
    b = Config(data_dir=Path("/tmp/x"), pools={})
    a.router.extra_secret_patterns.append("x")
    assert b.router.extra_secret_patterns == []


def test_fixture_loads() -> None:
    c = load_config(FIX)
    assert c.router.auto_alias == "big" and c.models["big"].local == "cpu"
    cl = c.clients["game"]
    assert cl.cloud_allowed and cl.default_priority is Priority.LOW
    assert c.task_types["summarize"].think is False


@pytest.mark.parametrize(
    "extra",
    [
        {"models": {"a": {"local": "nope"}}},
        {"models": {"b": {}}},  # pool alias 'a' not in models
        {"models": {"auto": {}, "a": {}}},
        {"router": {"auto_alias": "zzz"}},
        {"clients": {"c": {"key_sha256": H1, "allowed_models": ["zzz"]}}},
        {
            "clients": {
                "c": {
                    "key_sha256": H1,
                    "allowed_models": [],
                    "max_priority": "normal",
                    "default_priority": "urgent",
                }
            }
        },
        {"router": {"extra_secret_patterns": ["(unclosed"]}},
        {
            "clients": {
                "c": {"key_sha256": H1, "allowed_models": []},
                "d": {"key_sha256": H1, "allowed_models": []},
            }
        },
        {
            "api": {"admin_key_sha256": H1},
            "clients": {"c": {"key_sha256": H1, "allowed_models": []}},
        },
        {"api": {"sync_wait_s": 481}},
        {"clients": {"c": {"key_sha256": "short", "allowed_models": []}}},
        {"local": {"concurrency": 2}},
        {"unknown_field": 1},
    ],
)
def test_validators_reject(extra: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        cfg(**extra)


def test_valid_clients_and_star() -> None:
    c = cfg(clients={"c": {"key_sha256": H1, "allowed_models": ["*"]}},
            api={"admin_key_sha256": H2})  # fmt: skip
    assert c.clients["c"].max_priority is Priority.NORMAL
    assert c.clients["c"].cloud_allowed is False


def test_core_only_config_skips_pool_alias_check() -> None:
    Config.model_validate({"data_dir": "/tmp/x", "pools": {"p1": POOL}})
