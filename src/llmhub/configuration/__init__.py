"""Configuration: config.yaml + env, schema and range validation (stage 3a)."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from llmhub.configuration.loader import (
        ConfigError,
        load_config,
        load_instance_secret,
        load_label_secret,
        load_weights_ca,
        load_weights_secret,
    )
    from llmhub.configuration.models import (
        BudgetConfig,
        Config,
        DbConfig,
        LeaseConfig,
        PoolConfig,
        ProvidersConfig,
        QueueClassConfig,
        QueueConfig,
        RetryConfig,
        TimeoutsConfig,
    )
    from llmhub.configuration.weights_models import (
        InferClientConfig,
        InferConfig,
        WeightFileConfig,
        WeightsConfig,
        WeightSetConfig,
        WeightSourceConfig,
    )

# public name -> submodule; resolved lazily (PEP 562) so packages never import each
# other at import time (no cycles between lifecycle, queue, budget and cloud modules)
_EXPORTS: dict[str, str] = {
    "BudgetConfig": "models",
    "Config": "models",
    "DbConfig": "models",
    "LeaseConfig": "models",
    "PoolConfig": "models",
    "ProvidersConfig": "models",
    "QueueClassConfig": "models",
    "QueueConfig": "models",
    "RetryConfig": "models",
    "TimeoutsConfig": "models",
    "ConfigError": "loader",
    "load_config": "loader",
    "load_label_secret": "loader",
    "load_instance_secret": "loader",
    "load_weights_secret": "loader",
    "load_weights_ca": "loader",
    "InferClientConfig": "weights_models",
    "InferConfig": "weights_models",
    "WeightFileConfig": "weights_models",
    "WeightSetConfig": "weights_models",
    "WeightsConfig": "weights_models",
    "WeightSourceConfig": "weights_models",
}
__all__ = [
    "BudgetConfig",
    "Config",
    "ConfigError",
    "DbConfig",
    "InferClientConfig",
    "InferConfig",
    "LeaseConfig",
    "PoolConfig",
    "ProvidersConfig",
    "QueueClassConfig",
    "QueueConfig",
    "RetryConfig",
    "TimeoutsConfig",
    "WeightFileConfig",
    "WeightSetConfig",
    "WeightSourceConfig",
    "WeightsConfig",
    "load_config",
    "load_instance_secret",
    "load_label_secret",
    "load_weights_ca",
    "load_weights_secret",
]


def __getattr__(name: str) -> Any:
    mod = _EXPORTS.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{mod}"), name)
