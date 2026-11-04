"""Local llama-server inference: HTTP client, EWMA stats, ETA, local queue worker (stage 3b)."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from llmhub.local_inference.client import (
        LocalBackendClient,
        LocalChatResult,
        LocalError,
        ProbeResult,
        api_key_from_env,
        build_backends,
    )
    from llmhub.local_inference.eta import (
        local_eta_s,
        work_s,
    )
    from llmhub.local_inference.stats import (
        BackendStats,
        Ewma,
    )
    from llmhub.local_inference.worker import (
        LocalWorker,
    )

# public name -> submodule; resolved lazily (PEP 562): importing the package stays cheap and
# never pulls FastAPI/httpx into processes that only need a neighbour module
_EXPORTS: dict[str, str] = {
    "LocalBackendClient": "client",
    "LocalChatResult": "client",
    "LocalError": "client",
    "ProbeResult": "client",
    "api_key_from_env": "client",
    "build_backends": "client",
    "BackendStats": "stats",
    "Ewma": "stats",
    "local_eta_s": "eta",
    "work_s": "eta",
    "LocalWorker": "worker",
}
__all__ = [
    "BackendStats",
    "Ewma",
    "LocalBackendClient",
    "LocalChatResult",
    "LocalError",
    "LocalWorker",
    "ProbeResult",
    "api_key_from_env",
    "build_backends",
    "local_eta_s",
    "work_s",
]


def __getattr__(name: str) -> Any:
    mod = _EXPORTS.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{mod}"), name)
