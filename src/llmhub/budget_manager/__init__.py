"""Budget: pre-launch estimate, reservations, limits, warnings, client allocation (stage 3a)."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from llmhub.budget_manager.accounts import (
        handle_provider_error,
        last_balance,
        note_balance,
    )
    from llmhub.budget_manager.allocation import (
        allocate_session,
    )
    from llmhub.budget_manager.estimate import (
        JobLoad,
        LaunchEstimate,
        estimate_launch,
        t_cold_p90,
    )
    from llmhub.budget_manager.limits import (
        Gate,
        HoldExceedsSessionBudget,
        Stage,
        check_hold,
        check_job_on_running,
        check_launch,
        launch_gate,
        reserve_job_on_running,
        stage,
        warnings,
        windows,
    )
    from llmhub.budget_manager.reservations import (
        release_instance,
        release_ref,
        reserve,
    )

# public name -> submodule; resolved lazily (PEP 562) so packages never import each
# other at import time (no cycles between lifecycle, queue, budget and cloud modules)
_EXPORTS: dict[str, str] = {
    "JobLoad": "estimate",
    "LaunchEstimate": "estimate",
    "estimate_launch": "estimate",
    "t_cold_p90": "estimate",
    "Gate": "limits",
    "HoldExceedsSessionBudget": "limits",
    "Stage": "limits",
    "check_hold": "limits",
    "check_job_on_running": "limits",
    "check_launch": "limits",
    "launch_gate": "limits",
    "reserve_job_on_running": "limits",
    "stage": "limits",
    "warnings": "limits",
    "windows": "limits",
    "release_instance": "reservations",
    "release_ref": "reservations",
    "reserve": "reservations",
    "allocate_session": "allocation",
    "handle_provider_error": "accounts",
    "last_balance": "accounts",
    "note_balance": "accounts",
}
__all__ = [
    "Gate",
    "HoldExceedsSessionBudget",
    "JobLoad",
    "LaunchEstimate",
    "Stage",
    "allocate_session",
    "check_hold",
    "check_job_on_running",
    "check_launch",
    "estimate_launch",
    "handle_provider_error",
    "last_balance",
    "launch_gate",
    "note_balance",
    "release_instance",
    "release_ref",
    "reserve",
    "reserve_job_on_running",
    "stage",
    "t_cold_p90",
    "warnings",
    "windows",
]


def __getattr__(name: str) -> Any:
    mod = _EXPORTS.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{mod}"), name)
