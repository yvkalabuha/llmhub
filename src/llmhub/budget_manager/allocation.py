"""Attribution of a session's cost to clients (table cost_allocations).

work_c = sum of the client's job costs in the session; hold_idle = rate * time a hold/prewarm
lease kept the GPU while no job ran (to the lease holder); the remaining shared cost
(cold start, idle after jobs) is split proportionally to work_c; with no work at all it goes
to the holders, and without holders to `unattributed`.
"""

from __future__ import annotations

import json
import sqlite3

from llmhub.cost_tracker.ledger import session_cost
from llmhub.cost_tracker.txutil import join_or_open

UNATTRIBUTED = "unattributed"
ADMIN = "admin"


def _rate(inst: sqlite3.Row) -> float:
    disk = 0.0
    if inst["offer_json"]:
        disk = float(json.loads(inst["offer_json"]).get("disk_usd_h", 0.0))
    return (float(inst["price_usd_h"] or 0.0) + disk) / 3600.0


def _merge(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _subtract(
    base: tuple[float, float], cuts: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    pieces = [base]
    for ca, cb in cuts:
        nxt: list[tuple[float, float]] = []
        for a, b in pieces:
            if cb <= a or ca >= b:
                nxt.append((a, b))
                continue
            if ca > a:
                nxt.append((a, ca))
            if cb < b:
                nxt.append((cb, b))
        pieces = nxt
    return pieces


def allocate_session(conn: sqlite3.Connection, instance_id: str) -> dict[str, tuple[float, float]]:
    """client -> (work_usd, shared_usd); rewrites cost_allocations for the instance."""
    inst = conn.execute("SELECT * FROM instances WHERE id=?", (instance_id,)).fetchone()
    if inst is None:
        return {}
    total = session_cost(conn, instance_id)
    rate = _rate(inst)  # USD per second

    jobs = conn.execute(
        "SELECT client_id, cost_usd, started_at, finished_at FROM jobs"
        " WHERE instance_id=? AND started_at IS NOT NULL",
        (instance_id,),
    ).fetchall()
    work: dict[str, float] = {}
    busy: list[tuple[float, float]] = []
    for j in jobs:
        end = j["finished_at"] if j["finished_at"] is not None else inst["accrued_to"]
        if end is None:
            continue
        busy.append((float(j["started_at"]), float(end)))
        cost = j["cost_usd"]
        if cost is None:
            cost = rate * (end - j["started_at"])
        work[j["client_id"]] = work.get(j["client_id"], 0.0) + float(cost)
    busy = _merge(busy)

    life_end = float(inst["zero_confirmed_at"] or inst["accrued_to"] or inst["created_at"])
    leases = conn.execute(
        "SELECT client_id, created_at, closed_at, expires_at FROM leases"
        " WHERE pool=? AND kind IN ('hold','prewarm')",
        (inst["pool"],),
    ).fetchall()
    hold: dict[str, float] = {}
    for ls in leases:
        a = max(float(ls["created_at"]), float(inst["created_at"]))
        b = min(float(ls["closed_at"] or ls["expires_at"] or life_end), life_end)
        if b <= a:
            continue
        secs = sum(y - x for x, y in _subtract((a, b), busy))
        if secs > 0:
            holder = ls["client_id"] or ADMIN
            hold[holder] = hold.get(holder, 0.0) + rate * secs

    work_sum = sum(work.values())
    hold_sum = sum(hold.values())
    if work_sum + hold_sum > total and work_sum + hold_sum > 0:
        # overlapping leases or a stale estimate: never allocate more than was spent
        room = max(0.0, total - work_sum)
        scale = room / hold_sum if hold_sum > 0 else 0.0
        hold = {c: v * scale for c, v in hold.items()}
        hold_sum = sum(hold.values())
    shared = max(0.0, total - work_sum - hold_sum)

    result: dict[str, tuple[float, float]] = {}
    for c, w in work.items():
        part = shared * w / work_sum if work_sum > 0 else 0.0
        result[c] = (w, part + hold.get(c, 0.0))
    for c, h in hold.items():
        if c not in result:
            result[c] = (0.0, h)
    if work_sum <= 0 and shared > 0:
        if hold:
            for c, (_, h) in list(result.items()):
                result[c] = (0.0, h + shared * h / hold_sum if hold_sum > 0 else h)
        else:
            result[UNATTRIBUTED] = (0.0, shared)

    with join_or_open(conn):
        conn.execute("DELETE FROM cost_allocations WHERE instance_id=?", (instance_id,))
        for c, (w, s) in result.items():
            conn.execute(
                "INSERT INTO cost_allocations(instance_id, client_id, work_usd, shared_usd)"
                " VALUES (?,?,?,?)",
                (instance_id, c, w, s),
            )
    return result
