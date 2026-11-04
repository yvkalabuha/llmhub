"""Leases: implicit (live with a job), explicit (TTL + heartbeat), hold (admin), prewarm.

Live lease: `closed_at IS NULL AND (expires_at IS NULL OR expires_at > now)`. All functions join
the caller's transaction when one is open, or open their own.
"""

from __future__ import annotations

import enum
import sqlite3
import uuid
from collections.abc import Iterable

from llmhub.configuration.models import LeaseConfig
from llmhub.lifecycle_manager.safety import join_or_open, record_incident


class LeaseKind(enum.StrEnum):
    IMPLICIT = "implicit"
    EXPLICIT = "explicit"
    HOLD = "hold"
    PREWARM = "prewarm"


class LeaseError(ValueError):
    """Invalid lease request (TTL out of range, wrong kind)."""


class LeaseExpired(Exception):
    """renew() on a lease whose TTL has passed (T-369: HTTP 410)."""


class LeaseClosed(Exception):
    """renew() on a lease that was closed."""


def create_implicit(
    conn: sqlite3.Connection, job_id: str, pool: str, client_id: str | None, now: float
) -> str:
    lease_id = uuid.uuid4().hex[:12]
    with join_or_open(conn):
        conn.execute(
            "INSERT INTO leases(id, holder, client_id, kind, pool, job_id, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (lease_id, f"job:{job_id}", client_id, LeaseKind.IMPLICIT, pool, job_id, now),
        )
    return lease_id


def _insert_timed(
    conn: sqlite3.Connection,
    kind: LeaseKind,
    holder: str,
    client_id: str | None,
    pool: str,
    ttl_s: float,
    now: float,
) -> str:
    lease_id = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO leases(id, holder, client_id, kind, pool, ttl_s, expires_at, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (lease_id, holder, client_id, kind, pool, ttl_s, now + ttl_s, now),
    )
    return lease_id


def create_explicit(
    conn: sqlite3.Connection,
    *,
    holder: str,
    client_id: str | None,
    pool: str,
    ttl_s: float | None = None,
    now: float,
    prewarm: bool = False,
    cfg: LeaseConfig | None = None,
) -> str:
    """Explicit (or prewarm) lease; TTL default 600 s, allowed range 30..3600 s."""
    lc = cfg or LeaseConfig()
    ttl = lc.default_ttl_s if ttl_s is None else ttl_s
    if not lc.min_ttl_s <= ttl <= lc.max_ttl_s:
        raise LeaseError(f"ttl_s {ttl} outside [{lc.min_ttl_s}, {lc.max_ttl_s}]")
    kind = LeaseKind.PREWARM if prewarm else LeaseKind.EXPLICIT
    with join_or_open(conn):
        return _insert_timed(conn, kind, holder, client_id, pool, ttl, now)


def create_hold(
    conn: sqlite3.Connection,
    *,
    holder: str,
    pool: str,
    ttl_s: float,
    now: float,
    cfg: LeaseConfig | None = None,
) -> str:
    """Admin hold, ttl <= hold_max_ttl (8 h). Extends max_lifetime_at of the live instance."""
    lc = cfg or LeaseConfig()
    if not 0 < ttl_s <= lc.hold_max_ttl_s:
        raise LeaseError(f"hold ttl_s {ttl_s} outside (0, {lc.hold_max_ttl_s}]")
    with join_or_open(conn):
        lease_id = _insert_timed(conn, LeaseKind.HOLD, holder, None, pool, ttl_s, now)
        conn.execute(
            "UPDATE instances SET max_lifetime_at=max(max_lifetime_at, ?) WHERE pool=?"
            " AND state IN ('PROVISIONING','BOOTING','LOADING_MODEL','READY','BUSY','IDLE')",
            (now + ttl_s, pool),
        )
    return lease_id


def renew(conn: sqlite3.Connection, lease_id: str, now: float) -> float:
    """Heartbeat: expires_at = now + ttl_s. Returns the new expires_at."""
    with join_or_open(conn):
        row = conn.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()
        if row is None:
            raise LeaseClosed(lease_id)
        if row["closed_at"] is not None:
            raise LeaseClosed(lease_id)
        if row["kind"] == LeaseKind.IMPLICIT:
            raise LeaseError("implicit leases live with their job and cannot be renewed")
        if row["expires_at"] <= now:
            raise LeaseExpired(lease_id)
        expires = now + float(row["ttl_s"])
        if row["kind"] == LeaseKind.HOLD:  # a hold never outlives its 8 h cap
            expires = min(expires, float(row["created_at"]) + LeaseConfig().hold_max_ttl_s)
        conn.execute("UPDATE leases SET expires_at=?, renewed_at=? WHERE id=?",
                     (expires, now, lease_id))  # fmt: skip
    return float(expires)


def close(
    conn: sqlite3.Connection,
    lease_id: str,
    now: float,
    *,
    final: bool = False,
    reason: str = "closed",
) -> None:
    """Idempotent close. `final=True` means "the batch is done" (drain)."""
    with join_or_open(conn):
        conn.execute(
            "UPDATE leases SET closed_at=?, close_reason=?, final=? WHERE id=? AND closed_at IS NULL",
            (now, reason, int(final), lease_id),
        )


def close_for_job(conn: sqlite3.Connection, job_id: str, now: float) -> None:
    with join_or_open(conn):
        conn.execute(
            "UPDATE leases SET closed_at=?, close_reason='job_done'"
            " WHERE job_id=? AND kind='implicit' AND closed_at IS NULL",
            (now, job_id),
        )


def release_holds(conn: sqlite3.Connection, pool: str | None, now: float, reason: str) -> int:
    """Close live hold leases of a pool (all pools when None); returns how many."""
    sql = "UPDATE leases SET closed_at=?, close_reason=? WHERE kind='hold' AND closed_at IS NULL"
    params: list[object] = [now, reason]
    if pool is not None:
        sql += " AND pool=?"
        params.append(pool)
    with join_or_open(conn):
        return conn.execute(sql, params).rowcount


def expire_due(conn: sqlite3.Connection, now: float) -> list[str]:
    """Close leases whose TTL passed; one `lease_expired` info incident each."""
    with join_or_open(conn):
        rows = conn.execute(
            "SELECT id, kind, pool, holder FROM leases WHERE closed_at IS NULL"
            " AND expires_at IS NOT NULL AND expires_at <= ? ORDER BY expires_at, id",
            (now,),
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE leases SET closed_at=?, close_reason='expired' WHERE id=?",
                (now, r["id"]),
            )
            record_incident(
                conn, now, "lease_expired", "info", ref=r["id"], dedup_key=f"lease_expired:{r['id']}",
                details={"kind": r["kind"], "pool": r["pool"], "holder": r["holder"]},
            )  # fmt: skip
    return [str(r["id"]) for r in rows]


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


def drain_requested(conn: sqlite3.Connection, pool: str, since: float) -> bool:
    """A lease of the pool was closed with `final=True` at or after `since`."""
    row = conn.execute(
        "SELECT 1 FROM leases WHERE pool=? AND final=1 AND closed_at >= ? LIMIT 1", (pool, since)
    ).fetchone()
    return row is not None


def hold_ended_since(conn: sqlite3.Connection, pool: str, since: float) -> bool:
    """A hold of the pool expired or was released at or after `since`."""
    row = conn.execute(
        "SELECT 1 FROM leases WHERE pool=? AND kind='hold' AND closed_at >= ? LIMIT 1",
        (pool, since),
    ).fetchone()
    return row is not None
