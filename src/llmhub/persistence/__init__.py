"""SQLite (WAL) persistence: schema, migrations, integrity_check, backup (stage 3a)."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from llmhub.persistence.backup import (
        backup,
        backup_state_db,
        maybe_daily_backup,
    )
    from llmhub.persistence.db import (
        Busy,
        assert_not_in_tx,
        connect,
        migrate,
        tx,
    )
    from llmhub.persistence.integrity import (
        OpenResult,
        StateDbBusy,
        open_state_db,
    )
    from llmhub.persistence.state import (
        ReconcileRequired,
    )

# public name -> submodule; resolved lazily (PEP 562) so packages never import each
# other at import time (no cycles between lifecycle, queue, budget and cloud modules)
_EXPORTS: dict[str, str] = {
    "Busy": "db",
    "assert_not_in_tx": "db",
    "connect": "db",
    "migrate": "db",
    "tx": "db",
    "OpenResult": "integrity",
    "open_state_db": "integrity",
    "StateDbBusy": "integrity",
    "backup_state_db": "backup",
    "backup": "backup",
    "maybe_daily_backup": "backup",
    "ReconcileRequired": "state",
}
__all__ = [
    "Busy",
    "OpenResult",
    "ReconcileRequired",
    "StateDbBusy",
    "assert_not_in_tx",
    "backup",
    "backup_state_db",
    "connect",
    "maybe_daily_backup",
    "migrate",
    "open_state_db",
    "tx",
]


def __getattr__(name: str) -> Any:
    mod = _EXPORTS.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{mod}"), name)
