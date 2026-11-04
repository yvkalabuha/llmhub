"""R3: alias resolution (`auto`, QUALITY, unknown, forbidden)."""

from __future__ import annotations

from pathlib import Path

import pytest

from llmhub.configuration.api_models import LocalBackendConfig, ModelAliasConfig, RouterConfig
from llmhub.configuration.models import Config, PoolConfig
from llmhub.model_router.rules import RouteError, RouteErrorCode, resolve_alias


def make(tmp_path: Path) -> Config:
    pool = PoolConfig(
        name="p1", provider="mock", gpu="g", vram_gb=24, pp_tps=1000, tg_tps=50, image="i",
        disk_gb=50, models=["big", "mid"],
    )  # fmt: skip
    return Config(
        data_dir=tmp_path,
        pools={"p1": pool},
        local_backends={
            "b": LocalBackendConfig(
                url="http://x/v1", model="m", ctx_tokens=4096, pp_tps=10, tg_tps=5
            )
        },
        models={
            "small": ModelAliasConfig(local="b", quality_tier=1),
            "mid": ModelAliasConfig(local="b", quality_tier=2),
            "big": ModelAliasConfig(quality_tier=3),
        },
        router=RouterConfig(auto_alias="small"),
    )


def test_auto_expands_to_auto_alias(tmp_path: Path) -> None:
    cfg = make(tmp_path)
    for policy in ("ECONOMY", "BALANCED"):
        assert resolve_alias("auto", policy, cfg, lambda _a: True) == "small"  # type: ignore[arg-type]


def test_quality_auto_picks_highest_tier_with_cloud(tmp_path: Path) -> None:
    cfg = make(tmp_path)
    assert resolve_alias("auto", "QUALITY", cfg, lambda _a: True, cloud_allowed=True) == "big"
    assert resolve_alias("auto", "QUALITY", cfg, lambda a: a != "big", cloud_allowed=True) == "mid"


def test_quality_auto_without_cloud_allowed_uses_auto_alias(tmp_path: Path) -> None:
    assert resolve_alias("auto", "QUALITY", make(tmp_path), lambda _a: True) == "small"


def test_quality_auto_without_allowed_cloud_alias_falls_back(tmp_path: Path) -> None:
    cfg = make(tmp_path)
    assert (
        resolve_alias("auto", "QUALITY", cfg, lambda a: a == "small", cloud_allowed=True) == "small"
    )


def test_explicit_alias_passes(tmp_path: Path) -> None:
    assert resolve_alias("mid", "BALANCED", make(tmp_path), lambda _a: True) == "mid"


def test_unknown_alias_is_model_not_available(tmp_path: Path) -> None:
    with pytest.raises(RouteError) as ei:
        resolve_alias("nope", "BALANCED", make(tmp_path), lambda _a: True)
    assert ei.value.code == RouteErrorCode.MODEL_NOT_AVAILABLE


def test_denied_alias_is_forbidden(tmp_path: Path) -> None:
    with pytest.raises(RouteError) as ei:
        resolve_alias("big", "BALANCED", make(tmp_path), lambda a: a != "big")
    assert ei.value.code == RouteErrorCode.FORBIDDEN


def test_auto_without_auto_alias_is_not_available(tmp_path: Path) -> None:
    cfg = make(tmp_path)
    cfg.router = RouterConfig()
    with pytest.raises(RouteError) as ei:
        resolve_alias("auto", "BALANCED", cfg, lambda _a: True)
    assert ei.value.code == RouteErrorCode.MODEL_NOT_AVAILABLE


def test_auto_alias_denied_is_forbidden(tmp_path: Path) -> None:
    with pytest.raises(RouteError) as ei:
        resolve_alias("auto", "ECONOMY", make(tmp_path), lambda a: a != "small")
    assert ei.value.code == RouteErrorCode.FORBIDDEN
