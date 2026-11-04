"""Ledger: per-instance accrual (source=estimate) and spend over a window."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence

from llmhub.budget_manager.reservations import consume_fifo
from llmhub.cost_tracker.txutil import join_or_open


def _disk_rate_usd_h(inst: sqlite3.Row) -> float:
    raw = inst["offer_json"]
    if not raw:
        return 0.0
    try:
        return float(json.loads(raw).get("disk_usd_h", 0.0))
    except (ValueError, TypeError, AttributeError):
        return 0.0


def _accrue_one(conn: sqlite3.Connection, inst: sqlite3.Row, until: float, now: float) -> bool:
    start = inst["accrued_to"]
    price = inst["price_usd_h"]
    if start is None or price is None or until <= start:
        return False
    # CAS on accrued_to: a concurrent accruer (controller vs reaper) loses silently
    cur = conn.execute(
        "UPDATE instances SET accrued_to=? WHERE id=? AND accrued_to=?",
        (until, inst["id"], start),
    )
    if cur.rowcount == 0:
        return False
    dt = until - start
    total = 0.0
    for kind, rate in (("gpu", float(price)), ("disk", _disk_rate_usd_h(inst))):
        if rate <= 0:
            continue
        amount = rate * dt / 3600.0
        conn.execute(
            "INSERT OR IGNORE INTO ledger(instance_id, provider, pool, kind, from_ts, to_ts,"
            " rate, amount_usd, source, created_at) VALUES (?,?,?,?,?,?,?,?,'estimate',?)",
            (inst["id"], inst["provider"], inst["pool"], kind, start, until, rate / 3600.0,
             amount, now),
        )  # fmt: skip
        total += amount
    if total > 0:
        consume_fifo(conn, inst["id"], total)
    return True


def _until(inst: sqlite3.Row, now: float) -> float:
    z = inst["zero_confirmed_at"]
    return now if z is None else min(now, float(z))


def accrue(conn: sqlite3.Connection, now: float) -> int:
    """Per billable instance append ledger rows [accrued_to, now) and consume reservations.

    Billable = every state except STOPPED, including PENDING_DELETE and LEAKED (until
    zero_confirmed_at). Idempotent: a repeated call at the same `now` writes nothing.
    """
    n = 0
    with join_or_open(conn):
        rows = conn.execute(
            "SELECT * FROM instances WHERE accrued_to IS NOT NULL AND price_usd_h IS NOT NULL"
            " AND (state != 'STOPPED' OR (zero_confirmed_at IS NOT NULL"
            "                              AND accrued_to < zero_confirmed_at))"
        ).fetchall()
        for inst in rows:
            if _accrue_one(conn, inst, _until(inst, now), now):
                n += 1
    return n


def finalize(conn: sqlite3.Connection, instance_id: str, until: float) -> None:
    """Accrue an instance exactly up to `until` (e.g. zero_confirmed_at)."""
    with join_or_open(conn):
        inst = conn.execute("SELECT * FROM instances WHERE id=?", (instance_id,)).fetchone()
        if inst is not None:
            _accrue_one(conn, inst, until, until)


PRORATA_SQL = (
    "CASE WHEN to_ts > from_ts THEN"
    "  amount_usd * MAX(0, MIN(to_ts, :e) - MAX(from_ts, :s)) / (to_ts - from_ts)"
    " WHEN from_ts >= :s AND from_ts < :e THEN amount_usd ELSE 0 END"
)


def spent(
    conn: sqlite3.Connection,
    start: float,
    end: float,
    *,
    provider: str | None = None,
    pools: Sequence[str] | None = None,
) -> float:
    """Sum over providers of max(estimate in window, provider-billed in window).

    Rows straddling the window edges are pro-rated by overlap. `pools` restricts to ledger
    rows of those pools (provider rows are attributed to a pool only when their ref is known).
    """
    where = ["(to_ts > :s OR from_ts >= :s)", "from_ts < :e"]
    params: dict[str, object] = {"s": start, "e": end}
    if provider is not None:
        where.append("provider = :p")
        params["p"] = provider
    if pools is not None:
        keys = []
        for i, pool in enumerate(pools):
            params[f"pool{i}"] = pool
            keys.append(f":pool{i}")
        where.append(f"pool IN ({','.join(keys) or 'NULL'})")
    rows = conn.execute(
        f"SELECT provider, source, SUM({PRORATA_SQL}) AS total FROM ledger"
        f" WHERE {' AND '.join(where)} GROUP BY provider, source",
        params,
    ).fetchall()
    by_provider: dict[str, dict[str, float]] = {}
    for r in rows:
        by_provider.setdefault(r["provider"], {})[r["source"]] = float(r["total"] or 0.0)
    return sum(max(d.values()) for d in by_provider.values())


def session_cost(conn: sqlite3.Connection, instance_id: str) -> float:
    """Spend of one instance so far (max of estimate and provider rows with this id)."""
    rows = conn.execute(
        "SELECT source, SUM(amount_usd) AS t FROM ledger WHERE instance_id=? GROUP BY source",
        (instance_id,),
    ).fetchall()
    return max((float(r["t"]) for r in rows), default=0.0)
