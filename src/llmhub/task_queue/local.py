"""Local (pool IS NULL) queue operations and queue-wide expiry (T-369 stream G).

All functions run on the caller's connection and join an open transaction or open a short one.
`expire_overdue` covers local and cloud jobs alike.
"""

from __future__ import annotations

import contextlib
import sqlite3
from typing import Literal

from llmhub.budget_manager import reservations
from llmhub.cloud_manager.inference import JobRunResult
from llmhub.persistence.db import tx

_CLEAR_RUN = "instance_id=NULL, worker_id=NULL, worker_heartbeat_at=NULL"
_LIVE = "('submitted','queued','running')"


def _join_or_open(conn: sqlite3.Connection) -> contextlib.AbstractContextManager[object]:
    return contextlib.nullcontext() if conn.in_transaction else tx(conn)


def _close_lease(conn: sqlite3.Connection, job_id: str, now: float, reason: str) -> None:
    conn.execute(
        "UPDATE leases SET closed_at=?, close_reason=?"
        " WHERE job_id=? AND kind='implicit' AND closed_at IS NULL",
        (now, reason, job_id),
    )


def pick_next_local(conn: sqlite3.Connection, now: float) -> sqlite3.Row | None:
    """Priority rank, then round-robin by client (oldest last start first), then created_at."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT j.* FROM jobs j WHERE j.pool IS NULL AND j.status='queued'"
        " AND j.route LIKE 'local%' ORDER BY j.prio_rank,"
        " (SELECT MAX(k.started_at) FROM jobs k WHERE k.pool IS NULL AND k.client_id=j.client_id)"
        " IS NOT NULL,"
        " (SELECT MAX(k.started_at) FROM jobs k WHERE k.pool IS NULL AND k.client_id=j.client_id),"
        " j.created_at LIMIT 1"
    ).fetchone()
    return row


def mark_running_local(
    conn: sqlite3.Connection, job_id: str, worker_id: str, backend: str, now: float
) -> bool:
    """CAS queued -> running for a local job; False when somebody else got it."""
    cur = conn.execute(
        "UPDATE jobs SET status='running', instance_id=NULL, worker_id=?, backend=?,"
        " worker_heartbeat_at=?, started_at=?, attempts=attempts+1"
        " WHERE id=? AND status='queued' AND pool IS NULL",
        (worker_id, backend, now, now, job_id),
    )
    return cur.rowcount == 1


def complete_local(
    conn: sqlite3.Connection,
    job_id: str,
    worker_id: str,
    result: JobRunResult,
    now: float,
    res_path: str,
) -> bool:
    """CAS on status='running' AND worker_id; False = result discarded (job taken/cancelled).

    `cancel_requested=1` -> status 'cancelled'. Cost is 0.0 (local)."""
    with _join_or_open(conn):
        cur = conn.execute(
            "UPDATE jobs SET status=CASE WHEN cancel_requested=1 THEN 'cancelled'"
            " ELSE 'completed' END, tokens_in=?, tokens_out=?, finish_reason=?, cost_usd=0.0,"
            " finished_at=?, res_path=? WHERE id=? AND status='running' AND worker_id=?",
            (result.tokens_in, result.tokens_out, result.finish_reason, now, res_path, job_id,
             worker_id),
        )  # fmt: skip
        if cur.rowcount != 1:
            return False
        _close_lease(conn, job_id, now, "job_done")
        return True


def fail(
    conn: sqlite3.Connection,
    job_id: str,
    code: str,
    now: float,
    *,
    reason: str | None = None,
) -> None:
    """Terminal `failed` with `error_code`; closes the implicit lease, releases the reservation.
    No-op for a job that is already terminal."""
    with _join_or_open(conn):
        cur = conn.execute(
            f"UPDATE jobs SET status='failed', error_code=?,"
            f" fallback_reason=COALESCE(?, fallback_reason), finished_at=?, {_CLEAR_RUN}"
            f" WHERE id=? AND status IN {_LIVE}",
            (code, reason, now, job_id),
        )
        if cur.rowcount != 1:
            return
        reservations.release_ref(conn, "job", job_id, now, f"failed:{code}")
        _close_lease(conn, job_id, now, f"failed:{code}")


def requeue_local(
    conn: sqlite3.Connection,
    job_id: str,
    now: float,
    reason: str,
    *,
    max_attempts: int | None = None,
) -> Literal["queued", "failed"]:
    """Back to the queue, or `fail(<reason>)` once attempts are used up. Never falls back to
    the cloud route. `max_attempts` overrides the job's own limit (LocalConfig.max_attempts)."""
    with _join_or_open(conn):
        row = conn.execute(
            "SELECT status, attempts, max_attempts, cancel_requested FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if row is None or row["status"] not in ("running", "queued"):
            return "failed"
        if row["cancel_requested"]:
            conn.execute(
                f"UPDATE jobs SET status='cancelled', finished_at=?, {_CLEAR_RUN} WHERE id=?",
                (now, job_id),
            )
            _close_lease(conn, job_id, now, "cancelled")
            return "failed"
        limit = max_attempts if max_attempts is not None else int(row["max_attempts"])
        if int(row["attempts"]) >= limit:
            code = (
                reason
                if reason in ("local_unavailable", "upstream_timeout")
                else ("local_unavailable")
            )
            fail(conn, job_id, code, now)
            return "failed"
        conn.execute(
            f"UPDATE jobs SET status='queued', {_CLEAR_RUN}, error_code=? WHERE id=?",
            (reason, job_id),
        )
        return "queued"


def local_queue_ahead(
    conn: sqlite3.Connection, prio_rank: int, created_at: float
) -> list[sqlite3.Row]:
    """Queued/running local jobs that will run before a new job of `prio_rank` (for ETA)."""
    return conn.execute(
        "SELECT * FROM jobs WHERE pool IS NULL AND status IN ('queued','running')"
        " AND (status='running' OR prio_rank<? OR (prio_rank=? AND created_at<=?))"
        " ORDER BY status DESC, prio_rank, created_at",
        (prio_rank, prio_rank, created_at),
    ).fetchall()


def expire_overdue(conn: sqlite3.Connection, now: float) -> list[tuple[str, str]]:
    """Queued jobs (local and cloud) past `deadline_at` -> failed/deadline_exceeded, past
    `created_at + max_queue_wait_s` -> failed/queue_wait_exceeded. Returns (job_id, code)."""
    with _join_or_open(conn):
        rows = conn.execute(
            "SELECT id, deadline_at, created_at, max_queue_wait_s FROM jobs WHERE status='queued'"
            " AND ((deadline_at IS NOT NULL AND deadline_at<=?)"
            " OR (max_queue_wait_s IS NOT NULL AND created_at+max_queue_wait_s<=?))"
            " ORDER BY created_at",
            (now, now),
        ).fetchall()
        out: list[tuple[str, str]] = []
        for r in rows:
            code = (
                "deadline_exceeded"
                if r["deadline_at"] is not None and r["deadline_at"] <= now
                else "queue_wait_exceeded"
            )
            fail(conn, r["id"], code, now)
            out.append((r["id"], code))
        return out
