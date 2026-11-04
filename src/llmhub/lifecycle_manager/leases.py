"""Leases. S0 holds implicit leases and counting; stream C adds explicit/hold/prewarm."""

from __future__ import annotations

import enum
import sqlite3
import uuid
from collections.abc import Iterable


class LeaseKind(enum.StrEnum):
    IMPLICIT = "implicit"
    EXPLICIT = "explicit"
    HOLD = "hold"
    PREWARM = "prewarm"


def create_implicit(
    conn: sqlite3.Connection, job_id: str, pool: str, client_id: str | None, now: float
) -> str:
    lease_id = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO leases(id, holder, client_id, kind, pool, job_id, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (lease_id, f"job:{job_id}", client_id, LeaseKind.IMPLICIT, pool, job_id, now),
    )
    return lease_id


def close_for_job(conn: sqlite3.Connection, job_id: str, now: float) -> None:
    conn.execute(
        "UPDATE leases SET closed_at=?, close_reason='job_done'"
        " WHERE job_id=? AND kind='implicit' AND closed_at IS NULL",
        (now, job_id),
    )


def live_count(
    conn: sqlite3.Connection, pool: str, now: float, kinds: Iterable[LeaseKind] | None = None
) -> int:
    """Live: closed_at IS NULL AND (expires_at IS NULL OR expires_at > now)."""
    sql = (
        "SELECT count(*) FROM leases WHERE pool=? AND closed_at IS NULL"
        " AND (expires_at IS NULL OR expires_at > ?)"
    )
    params: list[object] = [pool, now]
    if kinds is not None:
        ks = [str(k) for k in kinds]
        sql += f" AND kind IN ({','.join('?' for _ in ks)})"
        params += ks
    return int(conn.execute(sql, params).fetchone()[0])
