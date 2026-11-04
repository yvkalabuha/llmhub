"""Provider account state: blocking on insufficient balance / quota. Resume is manual only."""

from __future__ import annotations

import json
import sqlite3

from llmhub.cost_tracker.txutil import add_incident, join_or_open
from llmhub.provider_adapters.base import NormalizedError, UnavailableReason

_BLOCKING = (UnavailableReason.INSUFFICIENT_BALANCE, UnavailableReason.QUOTA_EXCEEDED)


def _ensure(conn: sqlite3.Connection, provider: str) -> None:
    conn.execute("INSERT OR IGNORE INTO provider_accounts(provider) VALUES (?)", (provider,))


def is_blocked(conn: sqlite3.Connection, provider: str) -> str | None:
    row = conn.execute(
        "SELECT blocked_reason FROM provider_accounts WHERE provider=?", (provider,)
    ).fetchone()
    return None if row is None else row["blocked_reason"]


def block_provider(conn: sqlite3.Connection, provider: str, reason: str, now: float) -> None:
    """Block new launches for a provider: no retries, no auto top-up. Idempotent while blocked."""
    with join_or_open(conn):
        _ensure(conn, provider)
        cur = conn.execute(
            "UPDATE provider_accounts SET blocked_reason=?, blocked_at=?"
            " WHERE provider=? AND blocked_reason IS NULL",
            (reason, now, provider),
        )
        if cur.rowcount:
            add_incident(
                conn, now, "provider_blocked", "high", ref=provider,
                dedup_key=f"provider_blocked:{provider}:{reason}:{int(now)}",
                details_json=json.dumps({"reason": reason}),
            )  # fmt: skip


def resume_provider(conn: sqlite3.Connection, provider: str, now: float) -> None:
    """Manual unblock only (admin). Balance growth or time never unblock by themselves."""
    with join_or_open(conn):
        _ensure(conn, provider)
        conn.execute(
            "UPDATE provider_accounts SET blocked_reason=NULL, blocked_at=NULL WHERE provider=?",
            (provider,),
        )
        conn.execute(
            "UPDATE incidents SET resolved_at=? WHERE kind='provider_blocked' AND ref=?"
            " AND resolved_at IS NULL",
            (now, provider),
        )


def note_balance(conn: sqlite3.Connection, provider: str, usd: float, now: float) -> None:
    """Record the last known balance. Never lifts a block."""
    with join_or_open(conn):
        _ensure(conn, provider)
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
