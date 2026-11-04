"""Deterministic router: ECONOMY/BALANCED/QUALITY rules, secret detector, cloud view (stage 3b)."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from llmhub.model_router.rules import (
        CloudView,
        LocalView,
        RouteDecision,
        RouteError,
        RouteErrorCode,
        RouteRequest,
        decide,
        resolve_alias,
    )
    from llmhub.model_router.secrets import (
        detect_secrets,
    )
    from llmhub.model_router.views import (
        CloudState,
        EtaSource,
        retry_after_s,
    )

# public name -> submodule; resolved lazily (PEP 562): importing the package stays cheap and
# never pulls FastAPI/httpx into processes that only need a neighbour module
_EXPORTS: dict[str, str] = {
    "CloudView": "rules",
    "LocalView": "rules",
    "RouteDecision": "rules",
    "RouteError": "rules",
    "RouteErrorCode": "rules",
    "RouteRequest": "rules",
    "decide": "rules",
    "resolve_alias": "rules",
    "detect_secrets": "secrets",
    "CloudState": "views",
    "EtaSource": "views",
    "retry_after_s": "views",
}
__all__ = [
    "CloudState",
    "CloudView",
    "EtaSource",
    "LocalView",
    "RouteDecision",
    "RouteError",
    "RouteErrorCode",
    "RouteRequest",
    "decide",
    "detect_secrets",
    "resolve_alias",
    "retry_after_s",
]


def __getattr__(name: str) -> Any:
    mod = _EXPORTS.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{mod}"), name)
