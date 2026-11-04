"""Provider account state: blocking on insufficient balance / quota. Resume is manual only.

`block_provider`, `is_blocked` and `resume_provider` live in lifecycle_manager.safety (one API).
"""

from __future__ import annotations

import sqlite3

from llmhub.cost_tracker.txutil import join_or_open
from llmhub.lifecycle_manager.safety import block_provider, is_blocked, resume_provider
from llmhub.provider_adapters.base import NormalizedError, UnavailableReason

__all__ = [
    "block_provider", "handle_provider_error", "is_blocked", "last_balance", "note_balance",
    "resume_provider",
]  # fmt: skip

_BLOCKING = (UnavailableReason.INSUFFICIENT_BALANCE, UnavailableReason.QUOTA_EXCEEDED)


def note_balance(conn: sqlite3.Connection, provider: str, usd: float, now: float) -> None:
    """Record the last known balance. Never lifts a block."""
    with join_or_open(conn):
        conn.execute("INSERT OR IGNORE INTO provider_accounts(provider) VALUES (?)", (provider,))
        conn.execute(
            "UPDATE provider_accounts SET last_balance_usd=?, last_balance_at=? WHERE provider=?",
            (usd, now, provider),
        )


def last_balance(conn: sqlite3.Connection, provider: str) -> float | None:
    row = conn.execute(
        "SELECT last_balance_usd FROM provider_accounts WHERE provider=?", (provider,)
    ).fetchone()
    return None if row is None or row[0] is None else float(row[0])


def handle_provider_error(
    conn: sqlite3.Connection, provider: str, err: NormalizedError, now: float
) -> bool:
    """Insufficient balance / quota -> block the provider (True). Other errors: no effect."""
    if err.reason in _BLOCKING:
        block_provider(conn, provider, str(err.reason), now)
        return True
    return False
