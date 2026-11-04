"""Compare-and-set state transitions and instance insertion (plan sections 3.5, 5).

`transition` and `set_provider_ref` join the caller's transaction when one is open, or open
their own. `insert_provisioning` and `try_idle_to_stopping` are always one transaction.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any, Literal

from llmhub.budget_manager.limits import Gate
from llmhub.configuration.models import Config
from llmhub.lifecycle_manager import leases
from llmhub.lifecycle_manager.safety import blacklist_host, join_or_open, record_incident
from llmhub.lifecycle_manager.states import LIVE_IN_INDEX, State, check_edge
from llmhub.persistence.db import tx

Actor = Literal["controller", "reaper", "admin"]

# instances columns that `transition(**fields)` may set (besides state / state_since)
_FIELDS = frozenset(
    {
        "provider_ref", "offer_json", "host_id", "gpu", "price_usd_h", "endpoint",
        "tls_fingerprint", "create_called_at", "ready_at", "idle_since", "stop_after_current",
        "stop_reason", "destroy_requested_at", "destroy_accepted_at", "destroy_attempts",
        "next_destroy_at", "zero_confirmed_at", "attempts", "model_test_failures_json",
        "net_fail_since", "accrued_to", "last_error", "max_lifetime_at",
        "first_create_called_at", "last_create_called_at", "create_unknown",
    }
)  # fmt: skip


def insert_provisioning(
    conn: sqlite3.Connection,
    *,
    pool: str,
    provider: str,
    op_id: str,
    label: str,
    max_lifetime_at: float,
    now: float,
    gate: Callable[[sqlite3.Connection], Gate],
) -> str | None:
    """ONE tx: gate re-check, INSERT (E1) and transitions row. None on gate fail or UNIQUE.

    The `session` reservation is written by the gate (`limits.launch_gate`). A live hold of the
    pool extends `max_lifetime_at` to its `expires_at` (capped at `hold_max_ttl_s` from now,
    R1-8), and its reservation (made with no instance) is bound to the new instance (R1-2).
    """
    from llmhub.budget_manager.reservations import bind_pool_holds  # local: budget -> lifecycle

    instance_id = f"inst-{op_id}"
    try:
        with tx(conn):
            if not gate(conn).ok:
                return None
            max_lifetime_at = max(max_lifetime_at, leases.hold_lifetime_until(conn, pool, now))
            conn.execute(
                "INSERT INTO instances(id, op_id, pool, provider, label, state, state_since,"
                " created_at, max_lifetime_at, accrued_to) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (instance_id, op_id, pool, provider, label, State.PROVISIONING, now, now,
                 max_lifetime_at, now),
            )  # fmt: skip
            conn.execute(
                "INSERT INTO transitions(instance_id, ts, from_state, to_state, reason, actor)"
                " VALUES (?,?,?,?,?,?)",
                (instance_id, now, State.STOPPED, State.PROVISIONING, "E1", "controller"),
            )
            bind_pool_holds(conn, pool, instance_id)
    except sqlite3.IntegrityError:
        return None
    return instance_id


def set_provider_ref(
    conn: sqlite3.Connection,
    instance_id: str,
    ref: str,
    host_id: str | None,
    price_usd_h: float | None,
    now: float,
) -> State:
    """UPDATE by id WITHOUT a state CAS; returns the current state.

    A state other than PROVISIONING means the caller must destroy `ref` at once.
    """
    with join_or_open(conn):
        conn.execute(
            "UPDATE instances SET provider_ref=?, host_id=?, price_usd_h=? WHERE id=?",
            (ref, host_id, price_usd_h, instance_id),
        )
        row = conn.execute("SELECT state FROM instances WHERE id=?", (instance_id,)).fetchone()
    return State(row["state"])


def transition(
    conn: sqlite3.Connection,
    instance_id: str,
    src: State,
    dst: State,
    trigger: str,
    now: float,
    actor: Actor,
    **fields: Any,
) -> bool:
    """CAS `state=src -> dst` plus a `transitions` row. False when the race was lost.

    `trigger` is the edge id (E1..E22); `reason=` in fields overrides the stored reason.
    """
    check_edge(src, dst, trigger)
    reason = str(fields.pop("reason", trigger))
    unknown = set(fields) - _FIELDS
    if unknown:
        raise ValueError(f"unknown instance fields: {sorted(unknown)}")
    params: list[Any]
    if src == dst:  # E11 BUSY -> BUSY: no state_since reset, no transitions row
        sets = [f"{k}=?" for k in fields] or ["state=state"]
        params = [*fields.values(), instance_id, src]
    else:
        sets = ["state=?", "state_since=?", *(f"{k}=?" for k in fields)]
        params = [dst, now, *fields.values(), instance_id, src]
    with join_or_open(conn):
        cur = conn.execute(
            f"UPDATE instances SET {', '.join(sets)} WHERE id=? AND state=?",
            params,
        )
        if cur.rowcount != 1:
            return False
        if src == dst:
            return True
        conn.execute(
            "INSERT INTO transitions(instance_id, ts, from_state, to_state, reason, actor)"
            " VALUES (?,?,?,?,?,?)",
            (instance_id, now, src, dst, reason, actor),
        )
    return True


def live_instance(conn: sqlite3.Connection, pool: str) -> sqlite3.Row | None:
    marks = ",".join("?" for _ in LIVE_IN_INDEX)
    row: sqlite3.Row | None = conn.execute(
        f"SELECT * FROM instances WHERE pool=? AND state IN ({marks})",
        (pool, *sorted(LIVE_IN_INDEX)),
    ).fetchone()
    return row


def try_idle_to_stopping(
    conn: sqlite3.Connection, instance_id: str, now: float, reason: str
) -> Literal["stopping", "busy", "noop"]:
    """ONE tx: queue + leases re-check. IDLE -> STOPPING (E14), or IDLE -> BUSY (E13)."""
    with tx(conn):
        inst = conn.execute("SELECT * FROM instances WHERE id=?", (instance_id,)).fetchone()
        if inst is None or inst["state"] != State.IDLE:
            return "noop"
        queued = conn.execute(
            "SELECT count(*) FROM jobs WHERE pool=? AND status IN ('submitted','queued','running')",
            (inst["pool"],),
        ).fetchone()[0]
        if queued or leases.live_count(conn, inst["pool"], now) > 0:
            transition(conn, instance_id, State.IDLE, State.BUSY, "E13", now, "controller",
                       idle_since=None)  # fmt: skip
            return "busy"
        ok = transition(conn, instance_id, State.IDLE, State.STOPPING, "E14", now, "controller",
                        stop_reason=reason, reason=reason)  # fmt: skip
        return "stopping" if ok else "noop"


# --- running jobs on an instance ------------------------------------------------------------


def requeue_running_jobs(
    conn: sqlite3.Connection,
    now: float,
    *,
    instance_id: str | None = None,
    not_worker: str | None = None,
    count_attempt: bool,
    reason: str = "instance_lost",
) -> list[str]:
    """Put `running` jobs back to the queue through `task_queue.queue.requeue` (one owner).

    Select by instance and/or foreign worker_id. `count_attempt=False` (restart, kill) refunds
    the attempt and counts a restart; `count_attempt=True` keeps it, and a job at
    `max_attempts` goes to local fallback. Returns the ids put back to `queued` in the pool.
    """
    from llmhub.task_queue import queue  # local import: queue -> leases -> safety

    where = ["status='running'"]
    params: list[Any] = []
    if instance_id is not None:
        where.append("instance_id=?")
        params.append(instance_id)
    if not_worker is not None:
        where.append("worker_id IS NOT ?")
        params.append(not_worker)
    ids: list[str] = []
    with join_or_open(conn):
        rows = conn.execute(
            f"SELECT id FROM jobs WHERE {' AND '.join(where)} ORDER BY id", params
        ).fetchall()
        for r in rows:
            if not count_attempt:
                conn.execute("UPDATE jobs SET restarts=restarts+1 WHERE id=?", (r["id"],))
            out = queue.requeue(conn, r["id"], now, count_attempt=count_attempt, reason=reason)
            if out == "queued":
                ids.append(str(r["id"]))
    return ids


def running_count(conn: sqlite3.Connection, instance_id: str) -> int:
    return int(
        conn.execute(
            "SELECT count(*) FROM jobs WHERE instance_id=? AND status='running'", (instance_id,)
        ).fetchone()[0]
    )


def _queued_count(conn: sqlite3.Connection, pool: str) -> int:
    return int(
        conn.execute(
            "SELECT count(*) FROM jobs WHERE pool=? AND status IN ('submitted','queued')", (pool,)
        ).fetchone()[0]
    )


# --- idle decisions (E9, E12, E14, E16) -----------------------------------------------------


def idle_stop_reason(
    conn: sqlite3.Connection,
    inst: sqlite3.Row,
    now: float,
    cfg: Config,
    *,
    no_launch: bool = False,
) -> str | None:
    """Why an IDLE instance should be stopped now (E14 triggers), or None.

    The caller still passes the result through `try_idle_to_stopping`, which re-checks the
    queue and leases in one transaction.
    """
    pool = inst["pool"]
    since = float(inst["idle_since"] if inst["idle_since"] is not None else inst["state_since"])
    if now >= float(inst["max_lifetime_at"]):
        return "max_lifetime"
    pool_cfg = cfg.pools.get(pool)
    if pool_cfg is None:  # F-R2-3: the pool was removed from the config
        return "pool_removed"
    if no_launch:
        return "stage_no_launch"
    if leases.drain_requested(conn, pool, activity_since(conn, inst)):
        return "drain"
    if leases.hold_ended_since(conn, pool, since):
        return "hold_expired"
    if now >= since + pool_cfg.idle_timeout_s:
        return "idle_timeout"
    return None


def activity_since(conn: sqlite3.Connection, inst: sqlite3.Row) -> float:
    """Start of the instance's last busy period (its latest E8/E13), else `ready_at`, else
    `created_at`. A final lease close at or after it is a drain, even when the close came
    while the last job still ran (R1-3)."""
    row = conn.execute(
        "SELECT MAX(ts) FROM transitions WHERE instance_id=? AND to_state='BUSY'", (inst["id"],)
    ).fetchone()
    if row is not None and row[0] is not None:
        return float(row[0])
    base = inst["ready_at"] if inst["ready_at"] is not None else inst["created_at"]
    return float(base)


def idle_due_at(inst: sqlite3.Row, cfg: Config) -> float:
    """Wake-up time of the idle timer (idle_since + idle_timeout, or max_lifetime if sooner)."""
    since = float(inst["idle_since"] if inst["idle_since"] is not None else inst["state_since"])
    pool_cfg = cfg.pools.get(inst["pool"])
    timeout = pool_cfg.idle_timeout_s if pool_cfg is not None else 0.0
    return min(since + timeout, float(inst["max_lifetime_at"]))


def settle_idle(
    conn: sqlite3.Connection,
    inst_id: str,
    now: float,
    cfg: Config,
    *,
    no_launch: bool = False,
) -> Literal["active", "idle", "stopping", "noop"]:
    """READY/BUSY with no work -> IDLE (E9/E12); BUSY + stop_after_current -> STOPPING (E16);
    IDLE with a stop reason -> STOPPING (E14, drain right after E12 in the same call)."""
    with tx(conn):
        inst = conn.execute("SELECT * FROM instances WHERE id=?", (inst_id,)).fetchone()
        if inst is None:
            return "noop"
        state = State(inst["state"])
        pool = inst["pool"]
        if state == State.BUSY and inst["stop_after_current"] and not running_count(conn, inst_id):
            ok = transition(conn, inst_id, State.BUSY, State.STOPPING, "E16", now, "controller",
                            stop_reason=inst["stop_reason"] or "stop_after_current")  # fmt: skip
            return "stopping" if ok else "noop"
        if state in (State.READY, State.BUSY):
            if (
                running_count(conn, inst_id)
                or _queued_count(conn, pool)
                or leases.live_count(conn, pool, now)
            ):
                return "active"
            edge = "E9" if state == State.READY else "E12"
            if not transition(conn, inst_id, state, State.IDLE, edge, now, "controller",
                              idle_since=now):  # fmt: skip
                return "noop"
        elif state != State.IDLE:
            return "noop"
    inst = conn.execute("SELECT * FROM instances WHERE id=?", (inst_id,)).fetchone()
    reason = idle_stop_reason(conn, inst, now, cfg, no_launch=no_launch)
    if reason is not None:
        outcome = try_idle_to_stopping(conn, inst_id, now, reason)
        if outcome == "stopping":
            return "stopping"
        if outcome == "busy":  # queue or lease appeared: E13
            return "active"
    return "idle"


def request_stop_after_current(conn: sqlite3.Connection, instance_id: str, reason: str) -> None:
    """BUSY instance: finish the running jobs, then stop (E16); new jobs are not handed out."""
    with join_or_open(conn):
        conn.execute(
            "UPDATE instances SET stop_after_current=1, stop_reason=COALESCE(stop_reason, ?)"
            " WHERE id=?",
            (reason, instance_id),
        )


# --- loss, kill (E3/E5/E7/E10/E15/E17/E18) ---------------------------------------------------


def mark_lost(
    conn: sqlite3.Connection,
    instance_id: str,
    now: float,
    cause: str,
    actor: Actor = "controller",
    *,
    cfg: Config | None = None,
) -> bool:
    """The resource is gone/unreachable: go to ERROR through the edge of the current state.

    BOOTING -> E5 and LOADING_MODEL -> E7 blacklist the host; READY -> E10; BUSY/IDLE -> E17
    (running jobs are requeued with the attempt counted). The next step does ERROR -> STOPPING.
    """
    blacklist_s = cfg.retry.host_blacklist_s if cfg else 86400.0
    with tx(conn):
        inst = conn.execute("SELECT * FROM instances WHERE id=?", (instance_id,)).fetchone()
        if inst is None:
            return False
        state = State(inst["state"])
        edge = {
            State.PROVISIONING: "E3", State.BOOTING: "E5", State.LOADING_MODEL: "E7",
            State.READY: "E10", State.BUSY: "E17", State.IDLE: "E17",
        }.get(state)  # fmt: skip
        if edge is None:
            return False
        if not transition(conn, instance_id, state, State.ERROR, edge, now, actor,
                          last_error=cause, reason=cause):  # fmt: skip
            return False
        if state in (State.BOOTING, State.LOADING_MODEL):
            blacklist_host(conn, inst["provider"], inst["host_id"], now, blacklist_s, cause)
        if state == State.BUSY:
            requeue_running_jobs(conn, now, instance_id=instance_id, count_attempt=True)
        record_incident(
            conn, now, "instance_lost" if edge in ("E10", "E17") else "instance_error", "warning",
            ref=instance_id, dedup_key=f"lost:{instance_id}:{edge}", details={"cause": cause},
        )  # fmt: skip
    return True


def error_to_stopping(
    conn: sqlite3.Connection, now: float, actor: Actor = "controller"
) -> list[str]:
    """E18: every ERROR instance goes to STOPPING in the same step."""
    done: list[str] = []
    with tx(conn):
        for r in conn.execute(
            "SELECT id FROM instances WHERE state='ERROR' ORDER BY id"
        ).fetchall():
            if transition(conn, r["id"], State.ERROR, State.STOPPING, "E18", now, actor,
                          stop_reason="error"):  # fmt: skip
                done.append(str(r["id"]))
    return done


def kill_to_stopping(
    conn: sqlite3.Connection, now: float, actor: Actor = "admin", reason: str = "kill"
) -> list[str]:
    """Kill: every live instance -> STOPPING (E15, ERROR via E18); running jobs are requeued
    without counting an attempt; holds are released. Returns the instance ids."""
    done: list[str] = []
    with tx(conn):
        marks = ",".join("?" for _ in LIVE_IN_INDEX)
        rows = conn.execute(
            f"SELECT * FROM instances WHERE state IN ({marks}) AND state != 'STOPPING' ORDER BY id",
            sorted(LIVE_IN_INDEX),
        ).fetchall()
        for r in rows:
            src = State(r["state"])
            edge = "E18" if src == State.ERROR else "E15"
            if transition(conn, r["id"], src, State.STOPPING, edge, now, actor,
                          stop_reason=reason, reason=reason):  # fmt: skip
                requeue_running_jobs(conn, now, instance_id=r["id"], count_attempt=False)
                done.append(str(r["id"]))
        leases.release_holds(conn, None, now, reason)
    return done
