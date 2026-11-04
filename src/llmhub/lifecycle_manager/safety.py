"""Incidents, HALT, provider blocks and host blacklist: small DB helpers for the lifecycle.

All functions join the caller's transaction when one is open, or open their own. They only
write rows; nothing here talks to a provider. `details` must never hold request text or secrets.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

from llmhub.persistence.db import tx

Severity = Literal["info", "warning", "high", "critical"]


@contextmanager
def join_or_open(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    if conn.in_transaction:
        yield conn
    else:
        with tx(conn):
            yield conn


def record_incident(
    conn: sqlite3.Connection,
    now: float,
    kind: str,
    severity: Severity,
    *,
    ref: str | None = None,
    dedup_key: str | None = None,
    details: dict[str, Any] | None = None,
) -> bool:
    """INSERT OR IGNORE by `dedup_key`; True when a new row was written."""
    with join_or_open(conn):
        cur = conn.execute(
            "INSERT OR IGNORE INTO incidents(ts, kind, severity, ref, dedup_key, details_json)"
            " VALUES (?,?,?,?,?,?)",
            (now, kind, severity, ref, dedup_key, json.dumps(details or {}, sort_keys=True)),
        )
    return cur.rowcount == 1


def set_key(conn: sqlite3.Connection, key: str, value: str, now: float) -> None:
    with join_or_open(conn):
        conn.execute(
            "INSERT INTO controller_state(key, value, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, now),
        )


def get_key(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM controller_state WHERE key=?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def delete_key(conn: sqlite3.Connection, key: str) -> None:
    with join_or_open(conn):
        conn.execute("DELETE FROM controller_state WHERE key=?", (key,))


def halt(conn: sqlite3.Connection, reason: str, now: float) -> None:
    """HALTED: no launches until a manual resume. Idempotent; keeps the first reason."""
    with join_or_open(conn):
        if get_key(conn, "halted") != "1":
            set_key(conn, "halted", "1", now)
            set_key(conn, "halt_reason", reason, now)


def is_halted(conn: sqlite3.Connection) -> bool:
    return get_key(conn, "halted") == "1"


def block_provider(conn: sqlite3.Connection, provider: str, reason: str, now: float) -> None:
    """Block launches on a provider (no retries, no auto-resume); idempotent."""
    with join_or_open(conn):
        conn.execute(
            "INSERT INTO provider_accounts(provider, blocked_reason, blocked_at) VALUES (?,?,?)"
            " ON CONFLICT(provider) DO UPDATE SET"
            " blocked_reason=COALESCE(provider_accounts.blocked_reason, excluded.blocked_reason),"
            " blocked_at=COALESCE(provider_accounts.blocked_at, excluded.blocked_at)",
            (provider, reason, now),
        )


def provider_blocked(conn: sqlite3.Connection, provider: str) -> str | None:
    row = conn.execute(
        "SELECT blocked_reason FROM provider_accounts WHERE provider=?", (provider,)
    ).fetchone()
    return None if row is None else row["blocked_reason"]


def blacklist_host(
    conn: sqlite3.Connection,
    provider: str,
    host_id: str | None,
    now: float,
    ttl_s: float,
    reason: str,
) -> None:
    if host_id is None:
        return
    with join_or_open(conn):
        conn.execute(
            "INSERT OR REPLACE INTO host_blacklist(provider, host_id, until, reason)"
            " VALUES (?,?,?,?)",
            (provider, host_id, now + ttl_s, reason),
        )
