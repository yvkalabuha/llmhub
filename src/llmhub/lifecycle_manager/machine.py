"""Compare-and-set state transitions and instance insertion (plan sections 3.5, 5).

`transition` and `set_provider_ref` join the caller's transaction when one is open, or open
their own. `insert_provisioning` and `try_idle_to_stopping` are always one transaction.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Literal

from llmhub.budget_manager.limits import Gate
from llmhub.lifecycle_manager import leases
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
    }
)  # fmt: skip


@contextmanager
def _join_or_open(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    if conn.in_transaction:
        yield conn
    else:
        with tx(conn):
            yield conn


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

    The `session` reservation is added by stream E when budget_manager lands.
    """
    instance_id = f"inst-{op_id}"
    try:
        with tx(conn):
            if not gate(conn).ok:
                return None
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
    with _join_or_open(conn):
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
    sets = ["state=?", "state_since=?", *(f"{k}=?" for k in fields)]
    params: list[Any] = [dst, now, *fields.values(), instance_id, src]
    with _join_or_open(conn):
        cur = conn.execute(
            f"UPDATE instances SET {', '.join(sets)} WHERE id=? AND state=?",
            params,
        )
        if cur.rowcount != 1:
            return False
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
            "SELECT count(*) FROM jobs WHERE pool=? AND status='queued'", (inst["pool"],)
        ).fetchone()[0]
        if queued or leases.live_count(conn, inst["pool"], now) > 0:
            transition(conn, instance_id, State.IDLE, State.BUSY, "E13", now, "controller",
                       idle_since=None)  # fmt: skip
            return "busy"
        ok = transition(conn, instance_id, State.IDLE, State.STOPPING, "E14", now, "controller",
                        stop_reason=reason, reason=reason)  # fmt: skip
        return "stopping" if ok else "noop"
