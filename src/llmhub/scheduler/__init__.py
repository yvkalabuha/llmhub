"""Scheduler: pool start triggers, task dispatch, max_concurrency (stage 3a)."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from llmhub.scheduler.triggers import (
        QueuedJob,
        TriggerDecision,
        dispatch_capacity,
        effective_deadline,
        launch_trigger,
        load_queued,
        work_seconds,
    )

# public name -> submodule; resolved lazily (PEP 562) so packages never import each
# other at import time (no cycles between lifecycle, queue, budget and cloud modules)
_EXPORTS: dict[str, str] = {
    "QueuedJob": "triggers",
    "TriggerDecision": "triggers",
    "dispatch_capacity": "triggers",
    "effective_deadline": "triggers",
    "launch_trigger": "triggers",
    "load_queued": "triggers",
    "work_seconds": "triggers",
}
__all__ = [
    "QueuedJob",
    "TriggerDecision",
    "dispatch_capacity",
    "effective_deadline",
    "launch_trigger",
    "load_queued",
    "work_seconds",
]


def __getattr__(name: str) -> Any:
    mod = _EXPORTS.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{mod}"), name)
