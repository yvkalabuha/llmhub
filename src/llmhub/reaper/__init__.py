"""Reaper: separate-process watchdog, orphans, PENDING_DELETE/LEAKED, zero check, kill-switch (stage 3a)."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from llmhub.reaper.core import (
        Reaper,
        ReaperReport,
        SweepReport,
        orphan_sweep,
    )

# public name -> submodule; resolved lazily (PEP 562) so packages never import each
# other at import time (no cycles between lifecycle, queue, budget and cloud modules)
_EXPORTS: dict[str, str] = {
    "Reaper": "core",
    "ReaperReport": "core",
    "SweepReport": "core",
    "orphan_sweep": "core",
}
__all__ = [
    "Reaper",
    "ReaperReport",
    "SweepReport",
    "orphan_sweep",
]


def __getattr__(name: str) -> Any:
    mod = _EXPORTS.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{mod}"), name)
