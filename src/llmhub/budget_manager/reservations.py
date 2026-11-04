"""Budget reservations: open amounts that count toward `committed` until consumed or released.

An instance accrual consumes its open reservations FIFO (session first, then job, hold,
prewarm; by created_at), so reserved money is never counted twice (reservation + ledger).
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Literal

from llmhub.cost_tracker.txutil import join_or_open

Scope = Literal["session", "job", "hold", "prewarm"]
_SCOPE_ORDER = "CASE scope WHEN 'session' THEN 0 WHEN 'job' THEN 1 WHEN 'hold' THEN 2 ELSE 3 END"


def reserve(
    conn: sqlite3.Connection,
    scope: Scope,
    ref_id: str,
    amount: float,
    now: float,
    instance_id: str | None = None,
) -> str:
    rid = f"res-{uuid.uuid4().hex[:16]}"
    with join_or_open(conn):
        conn.execute(
            "INSERT INTO reservations(id, scope, ref_id, instance_id, amount_usd, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (rid, scope, ref_id, instance_id, max(0.0, amount), now),
        )
    return rid


def remainder(row: sqlite3.Row) -> float:
    """max(0, amount - consumed) of a reservations row."""
    return max(0.0, float(row["amount_usd"]) - float(row["consumed_usd"]))


def release(conn: sqlite3.Connection, reservation_id: str, now: float, reason: str) -> None:
    with join_or_open(conn):
        conn.execute(
            "UPDATE reservations SET released_at=?, release_reason=?"
            " WHERE id=? AND released_at IS NULL",
            (now, reason, reservation_id),
        )


def release_ref(
    conn: sqlite3.Connection, scope: Scope, ref_id: str, now: float, reason: str
) -> int:
    """Release open reservations of a scope by ref (`<ref>` and `<ref>:idle` for jobs)."""
    with join_or_open(conn):
        cur = conn.execute(
            "UPDATE reservations SET released_at=?, release_reason=?"
            " WHERE scope=? AND (ref_id=? OR ref_id=?) AND released_at IS NULL",
            (now, reason, scope, ref_id, f"{ref_id}:idle"),
        )
    return cur.rowcount


def release_instance(conn: sqlite3.Connection, instance_id: str, now: float, reason: str) -> int:
    """Close every open reservation of an instance (E20/E22 and similar)."""
    with join_or_open(conn):
        cur = conn.execute(
            "UPDATE reservations SET released_at=?, release_reason=?"
            " WHERE instance_id=? AND released_at IS NULL",
            (now, reason, instance_id),
        )
    return cur.rowcount


def release_scopes(
    conn: sqlite3.Connection, scopes: tuple[Scope, ...], now: float, reason: str
) -> int:
    """Close all open reservations of given scopes (hold/prewarm at 100 % of the budget)."""
    marks = ",".join("?" * len(scopes))
    with join_or_open(conn):
        cur = conn.execute(
            f"UPDATE reservations SET released_at=?, release_reason=?"
            f" WHERE scope IN ({marks}) AND released_at IS NULL",
            (now, reason, *scopes),
        )
    return cur.rowcount


def open_remainder_total(
    conn: sqlite3.Connection,
    *,
    instance_id: str | None = None,
    pools: tuple[str, ...] | None = None,
) -> float:
    sql = "SELECT COALESCE(SUM(MAX(0, r.amount_usd - r.consumed_usd)), 0) FROM reservations r"
    where = ["r.released_at IS NULL"]
    params: list[object] = []
    if instance_id is not None:
        where.append("r.instance_id=?")
        params.append(instance_id)
    if pools is not None:
        sql += " JOIN instances i ON i.id = r.instance_id"
        where.append(f"i.pool IN ({','.join('?' * len(pools))})")
        params.extend(pools)
    row = conn.execute(f"{sql} WHERE {' AND '.join(where)}", params).fetchone()
    return float(row[0])


def consume_fifo(conn: sqlite3.Connection, instance_id: str, amount: float) -> float:
    """Accrual of an instance consumes its open reservations FIFO; returns the unconsumed rest.

    The rest simply lies in the ledger (spend above the reservations).
    """
    left = amount
    with join_or_open(conn):
        rows = conn.execute(
            "SELECT * FROM reservations WHERE instance_id=? AND released_at IS NULL"
            f" ORDER BY {_SCOPE_ORDER}, created_at, rowid",
            (instance_id,),
        ).fetchall()
        for row in rows:
            if left <= 1e-12:
                break
            take = min(left, remainder(row))
            if take <= 0:
                continue
            conn.execute(
                "UPDATE reservations SET consumed_usd = consumed_usd + ? WHERE id=?",
                (take, row["id"]),
            )
            left -= take
    return max(0.0, left)
