"""Cloud pools, deployment profiles, offer selection (stage 3a)."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from llmhub.cloud_manager.inference import (
        InferenceBackend,
        InstanceLost,
        JobRunResult,
        JobRunSpec,
        NetworkLost,
    )
    from llmhub.cloud_manager.offers import (
        select_offer,
    )
    from llmhub.cloud_manager.pools import (
        PoolRuntime,
        resolve_pool,
    )
    from llmhub.cloud_manager.provisioner import (
        CreateOutcome,
        create_or_adopt,
        find_duplicates,
        provision_step,
    )

# public name -> submodule; resolved lazily (PEP 562) so packages never import each
# other at import time (no cycles between lifecycle, queue, budget and cloud modules)
_EXPORTS: dict[str, str] = {
    "PoolRuntime": "pools",
    "resolve_pool": "pools",
    "select_offer": "offers",
    "CreateOutcome": "provisioner",
    "create_or_adopt": "provisioner",
    "find_duplicates": "provisioner",
    "provision_step": "provisioner",
    "InferenceBackend": "inference",
    "InstanceLost": "inference",
    "JobRunResult": "inference",
    "JobRunSpec": "inference",
    "NetworkLost": "inference",
}
__all__ = [
    "CreateOutcome",
    "InferenceBackend",
    "InstanceLost",
    "JobRunResult",
    "JobRunSpec",
    "NetworkLost",
    "PoolRuntime",
    "create_or_adopt",
    "find_duplicates",
    "provision_step",
    "resolve_pool",
    "select_offer",
]


def __getattr__(name: str) -> Any:
    mod = _EXPORTS.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{mod}"), name)
