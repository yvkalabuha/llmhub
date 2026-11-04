"""Cost tracker: ledger, reconciliation with provider balance and billing (stage 3a)."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from llmhub.cost_tracker.ledger import (
        accrue,
        finalize,
        session_cost,
        spent,
    )
    from llmhub.cost_tracker.reconcile import (
        ReconcileResult,
        ensure_resume_allowed,
        reconcile,
        reconcile_due,
        reload_billing,
    )

# public name -> submodule; resolved lazily (PEP 562) so packages never import each
# other at import time (no cycles between lifecycle, queue, budget and cloud modules)
_EXPORTS: dict[str, str] = {
    "accrue": "ledger",
    "finalize": "ledger",
    "session_cost": "ledger",
    "spent": "ledger",
    "ReconcileResult": "reconcile",
    "ensure_resume_allowed": "reconcile",
    "reconcile": "reconcile",
    "reconcile_due": "reconcile",
    "reload_billing": "reconcile",
}
__all__ = [
    "ReconcileResult",
    "accrue",
    "ensure_resume_allowed",
    "finalize",
    "reconcile",
    "reconcile_due",
    "reload_billing",
    "session_cost",
    "spent",
]


def __getattr__(name: str) -> Any:
    mod = _EXPORTS.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{mod}"), name)
