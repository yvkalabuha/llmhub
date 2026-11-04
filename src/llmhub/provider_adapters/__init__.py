"""Provider adapters: ProviderAdapter protocol, Capability, Mock (stage 3a), RunPod (T-372)."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from llmhub.provider_adapters.base import (
        Balance,
        BillingRecord,
        Capability,
        DestroyResult,
        InstanceSpec,
        NormalizedError,
        Offer,
        OfferQuery,
        ProviderAdapter,
        ProviderError,
        RemoteResource,
        RemoteStatus,
        ResourceKind,
        UnavailableReason,
    )
    from llmhub.provider_adapters.contract import (
        CONTRACT_ITEMS,
        AdapterHarness,
        run_contract,
    )
    from llmhub.provider_adapters.factory import (
        aclose_adapters,
        build_provider_adapters,
    )
    from llmhub.provider_adapters.guarded import (
        CircuitBreaker,
        GuardedAdapter,
    )
    from llmhub.provider_adapters.labels import (
        make_label,
        new_op_id,
        parse_label,
        verify_label,
    )
    from llmhub.provider_adapters.mock import (
        Fault,
        MockKnobs,
        MockProvider,
    )
    from llmhub.provider_adapters.mock_inference import (
        MockInference,
        MockInferenceKnobs,
    )
    from llmhub.provider_adapters.mock_store import (
        MockStore,
    )
    from llmhub.provider_adapters.runpod import (
        RunpodAdapter,
    )
    from llmhub.provider_adapters.runpod_api import (
        RunpodApi,
        RunpodHttpError,
        RunpodParseError,
    )

# public name -> submodule; resolved lazily (PEP 562) so packages never import each
# other at import time (no cycles between lifecycle, queue, budget and cloud modules)
_EXPORTS: dict[str, str] = {
    "Balance": "base",
    "BillingRecord": "base",
    "Capability": "base",
    "DestroyResult": "base",
    "InstanceSpec": "base",
    "NormalizedError": "base",
    "Offer": "base",
    "OfferQuery": "base",
    "ProviderAdapter": "base",
    "ProviderError": "base",
    "RemoteResource": "base",
    "RemoteStatus": "base",
    "ResourceKind": "base",
    "UnavailableReason": "base",
    "make_label": "labels",
    "new_op_id": "labels",
    "parse_label": "labels",
    "verify_label": "labels",
    "CircuitBreaker": "guarded",
    "GuardedAdapter": "guarded",
    "Fault": "mock",
    "MockKnobs": "mock",
    "MockProvider": "mock",
    "MockStore": "mock_store",
    "MockInference": "mock_inference",
    "MockInferenceKnobs": "mock_inference",
    "AdapterHarness": "contract",
    "CONTRACT_ITEMS": "contract",
    "run_contract": "contract",
    "aclose_adapters": "factory",
    "build_provider_adapters": "factory",
    "RunpodAdapter": "runpod",
    "RunpodApi": "runpod_api",
    "RunpodHttpError": "runpod_api",
    "RunpodParseError": "runpod_api",
}
__all__ = [
    "CONTRACT_ITEMS",
    "AdapterHarness",
    "Balance",
    "BillingRecord",
    "Capability",
    "CircuitBreaker",
    "DestroyResult",
    "Fault",
    "GuardedAdapter",
    "InstanceSpec",
    "MockInference",
    "MockInferenceKnobs",
    "MockKnobs",
    "MockProvider",
    "MockStore",
    "NormalizedError",
    "Offer",
    "OfferQuery",
    "ProviderAdapter",
    "ProviderError",
    "RemoteResource",
    "RemoteStatus",
    "ResourceKind",
    "RunpodAdapter",
    "RunpodApi",
    "RunpodHttpError",
    "RunpodParseError",
    "UnavailableReason",
    "aclose_adapters",
    "build_provider_adapters",
    "make_label",
    "new_op_id",
    "parse_label",
    "run_contract",
    "verify_label",
]


def __getattr__(name: str) -> Any:
    mod = _EXPORTS.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{mod}"), name)
