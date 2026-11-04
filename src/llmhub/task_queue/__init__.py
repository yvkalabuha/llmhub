"""Task queue: priorities, batching, idempotency, per-client round-robin (stage 3a)."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from llmhub.task_queue.bodies import (
        BodyStore,
    )
    from llmhub.task_queue.models import (
        IdempotencyConflict,
        JobSpec,
        JobStatus,
        Priority,
        SubmitResult,
    )
    from llmhub.task_queue.queue import (
        cancel,
        fallback_local,
        requeue,
        submit,
    )

# public name -> submodule; resolved lazily (PEP 562) so packages never import each
# other at import time (no cycles between lifecycle, queue, budget and cloud modules)
_EXPORTS: dict[str, str] = {
    "IdempotencyConflict": "models",
    "JobSpec": "models",
    "JobStatus": "models",
    "Priority": "models",
    "SubmitResult": "models",
    "BodyStore": "bodies",
    "cancel": "queue",
    "fallback_local": "queue",
    "requeue": "queue",
    "submit": "queue",
}
__all__ = [
    "BodyStore",
    "IdempotencyConflict",
    "JobSpec",
    "JobStatus",
    "Priority",
    "SubmitResult",
    "cancel",
    "fallback_local",
    "requeue",
    "submit",
]


def __getattr__(name: str) -> Any:
    mod = _EXPORTS.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{mod}"), name)
