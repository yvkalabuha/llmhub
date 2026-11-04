"""Pool runtime binding and model alias resolution."""

from __future__ import annotations

from dataclasses import dataclass

from llmhub.cloud_manager.inference import InferenceBackend
from llmhub.configuration.models import Config, PoolConfig
from llmhub.provider_adapters.base import ProviderAdapter


@dataclass(frozen=True)
class PoolRuntime:
    cfg: PoolConfig
    adapter: ProviderAdapter
    inference: InferenceBackend


def resolve_pool(model_alias: str, cfg: Config) -> str:
    for name, pool in cfg.pools.items():
        if model_alias in pool.models:
            return name
    raise KeyError(model_alias)
