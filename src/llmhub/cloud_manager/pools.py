"""Pool runtime binding and model alias resolution."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from llmhub.cloud_manager.inference import InferenceBackend
from llmhub.configuration.models import Config, PoolConfig
from llmhub.provider_adapters.base import ProviderAdapter

if TYPE_CHECKING:
    from llmhub.cloud_manager.instance_env import InstanceMaterial


@dataclass(frozen=True)
class PoolRuntime:
    cfg: PoolConfig
    adapter: ProviderAdapter
    inference: InferenceBackend
    # pod env + secrets of an instance row (T-370); None -> empty env (Mock pools, T-368)
    material: Callable[[sqlite3.Row], InstanceMaterial] | None = None


def resolve_pool(model_alias: str, cfg: Config) -> str:
    for name, pool in cfg.pools.items():
        if model_alias in pool.models:
            return name
    raise KeyError(model_alias)
