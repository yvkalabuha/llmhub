"""FastAPI gateway: OpenAI-compatible API, tasks, leases, admin, auth, errors (stage 3b)."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from llmhub.api_gateway.app import (
        LoopLag,
        Runtime,
        create_app,
    )
    from llmhub.api_gateway.auth import (
        ClientRegistry,
        Principal,
        generate_key,
        hash_key,
        install_sighup,
    )
    from llmhub.api_gateway.errors import (
        ApiError,
        ErrorCode,
    )
    from llmhub.api_gateway.logging import (
        SecretMaskFilter,
        configure_logging,
        mask,
    )
    from llmhub.api_gateway.submission import (
        Submitter,
        canonical_body,
    )
    from llmhub.api_gateway.waiter import (
        JobWaiter,
    )

# public name -> submodule; resolved lazily (PEP 562): importing the package stays cheap and
# never pulls FastAPI/httpx into processes that only need a neighbour module
_EXPORTS: dict[str, str] = {
    "LoopLag": "app",
    "Runtime": "app",
    "create_app": "app",
    "ClientRegistry": "auth",
    "Principal": "auth",
    "generate_key": "auth",
    "hash_key": "auth",
    "install_sighup": "auth",
    "ApiError": "errors",
    "ErrorCode": "errors",
    "JobWaiter": "waiter",
    "Submitter": "submission",
    "canonical_body": "submission",
    "SecretMaskFilter": "logging",
    "configure_logging": "logging",
    "mask": "logging",
}
__all__ = [
    "ApiError",
    "ClientRegistry",
    "ErrorCode",
    "JobWaiter",
    "LoopLag",
    "Principal",
    "Runtime",
    "SecretMaskFilter",
    "Submitter",
    "canonical_body",
    "configure_logging",
    "create_app",
    "generate_key",
    "hash_key",
    "install_sighup",
    "mask",
]


def __getattr__(name: str) -> Any:
    mod = _EXPORTS.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{mod}"), name)
