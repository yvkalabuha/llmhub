"""GPU lifecycle state machine, leases, idle, hold, recovery (stage 3a)."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from llmhub.lifecycle_manager.leases import (
        LeaseClosed,
        LeaseError,
        LeaseExpired,
        LeaseKind,
    )
    from llmhub.lifecycle_manager.machine import (
        insert_provisioning,
        live_instance,
        settle_idle,
        transition,
        try_idle_to_stopping,
    )
    from llmhub.lifecycle_manager.recovery import (
        ReconcileReport,
        reconcile_on_start,
    )
    from llmhub.lifecycle_manager.safety import (
        block_provider,
        halt,
        is_blocked,
        is_halted,
        record_incident,
        resume_provider,
    )
    from llmhub.lifecycle_manager.states import (
        BILLABLE,
        LIVE_IN_INDEX,
        TRANSITIONS,
        Edge,
        IllegalTransition,
        State,
        check_edge,
    )
    from llmhub.lifecycle_manager.teardown import (
        adopt_ref_or_destroy,
        confirm_zero,
        mark_leaks,
        request_destroy,
    )
    from llmhub.lifecycle_manager.timeouts import (
        apply_timeouts,
        check_alive,
    )

# public name -> submodule; resolved lazily (PEP 562) so packages never import each
# other at import time (no cycles between lifecycle, queue, budget and cloud modules)
_EXPORTS: dict[str, str] = {
    "BILLABLE": "states",
    "LIVE_IN_INDEX": "states",
    "TRANSITIONS": "states",
    "Edge": "states",
    "IllegalTransition": "states",
    "State": "states",
    "check_edge": "states",
    "insert_provisioning": "machine",
    "live_instance": "machine",
    "settle_idle": "machine",
    "transition": "machine",
    "try_idle_to_stopping": "machine",
    "LeaseClosed": "leases",
    "LeaseError": "leases",
    "LeaseExpired": "leases",
    "LeaseKind": "leases",
    "adopt_ref_or_destroy": "teardown",
    "confirm_zero": "teardown",
    "mark_leaks": "teardown",
    "request_destroy": "teardown",
    "apply_timeouts": "timeouts",
    "check_alive": "timeouts",
    "ReconcileReport": "recovery",
    "reconcile_on_start": "recovery",
    "block_provider": "safety",
    "halt": "safety",
    "is_blocked": "safety",
    "is_halted": "safety",
    "record_incident": "safety",
    "resume_provider": "safety",
}
__all__ = [
    "BILLABLE",
    "LIVE_IN_INDEX",
    "TRANSITIONS",
    "Edge",
    "IllegalTransition",
    "LeaseClosed",
    "LeaseError",
    "LeaseExpired",
    "LeaseKind",
    "ReconcileReport",
    "State",
    "adopt_ref_or_destroy",
    "apply_timeouts",
    "block_provider",
    "check_alive",
    "check_edge",
    "confirm_zero",
    "halt",
    "insert_provisioning",
    "is_blocked",
    "is_halted",
    "live_instance",
    "mark_leaks",
    "reconcile_on_start",
    "record_incident",
    "request_destroy",
    "resume_provider",
    "settle_idle",
    "transition",
    "try_idle_to_stopping",
]


def __getattr__(name: str) -> Any:
    mod = _EXPORTS.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{mod}"), name)
