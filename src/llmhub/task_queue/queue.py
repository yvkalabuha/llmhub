"""Job queue operations. S0 implements the happy path; stream D completes idempotency,
dedup, requeue, cancel, fallback_local and purge_expired."""

from __future__ import annotations

import contextlib
import hashlib
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from llmhub.budget_manager import reservations
from llmhub.cloud_manager.inference import JobRunResult
from llmhub.configuration.models import Config
from llmhub.lifecycle_manager import leases
from llmhub.lifecycle_manager.safety import record_incident
from llmhub.persistence.db import tx
from llmhub.provider_adapters.base import UnavailableReason
from llmhub.task_queue import local
from llmhub.task_queue.bodies import BodyStore
from llmhub.task_queue.models import IdempotencyConflict, JobSpec, JobStatus, SubmitResult


def _join_or_open(conn: sqlite3.Connection) -> contextlib.AbstractContextManager[object]:
    """Join an open transaction or open a short one (same convention as the state machine)."""
    return contextlib.nullcontext() if conn.in_transaction else tx(conn)


def _release_expired_keys(conn: sqlite3.Connection, now: float) -> None:
    conn.execute(
        "UPDATE jobs SET idem_key=NULL, idem_expires_at=NULL"
        " WHERE idem_key IS NOT NULL AND idem_expires_at IS NOT NULL AND idem_expires_at<=?",
        (now,),
    )


def _find_explicit(conn: sqlite3.Connection, client_id: str, key: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT id, body_sha256, status, error_code, fallback_reason FROM jobs"
        " WHERE client_id=? AND idem_key=?",
        (client_id, key),
    ).fetchone()
    return row


def _find_implicit(
    conn: sqlite3.Connection, client_id: str, sha: str, now: float, window_s: float
) -> sqlite3.Row | None:
    """Same client + body: still in flight, or finished within the dedup window."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT id, status FROM jobs WHERE client_id=? AND body_sha256=? AND ("
        " status IN ('submitted','queued','running')"
        " OR (status='completed' AND finished_at IS NOT NULL AND finished_at>=?))"
        " ORDER BY created_at DESC LIMIT 1",
        (client_id, sha, now - window_s),
    ).fetchone()
    return row


def _recovery_meta(spec: JobSpec) -> dict[str, object]:
    """Everything but the body, so safe mode can re-queue a job whose row was lost (F)."""
    meta = {k: getattr(spec, k) for k in spec.__dataclass_fields__ if k != "body"}
    meta["priority"] = spec.priority.value
    return meta


def submit(
    conn: sqlite3.Connection,
    bodies: BodyStore,
    spec: JobSpec,
    now: float,
    cfg: Config,
    *,
    retry_failed: Callable[[sqlite3.Row], bool] | None = None,
) -> SubmitResult:
    """Idempotent submit.

    Explicit `idem_key`: same body -> the existing job, other body -> IdempotencyConflict
    (stored for `cfg.queue.idem_ttl_s`, 7 days). No key: implicit dedup by sha256(client+body)
    while the job is queued/running and for `implicit_dedup_window_s` after completion.
    `retry_failed(row)` (id, status, error_code, fallback_reason) True for a failed job of the
    same key: the key is released from that job and a new job is created (a retryable failure
    must be retryable with the same key).
    """
    sha = hashlib.sha256(spec.client_id.encode() + b"\x00" + spec.body).hexdigest()
    job_id = uuid.uuid4().hex[:12]
    # The body file is written before the transaction; removed again on a duplicate.
    path = bodies.write_request(job_id, spec.body, _recovery_meta(spec))
    out = spec.max_tokens or cfg.task_type_max_tokens.get(
        spec.task_type or "default", cfg.task_type_max_tokens["default"]
    )
    try:
        with tx(conn):
            _release_expired_keys(conn, now)
            dup: sqlite3.Row | None = None
            if spec.idem_key is not None:
                dup = _find_explicit(conn, spec.client_id, spec.idem_key)
                if dup is not None and dup["body_sha256"] != sha:
                    raise IdempotencyConflict(dup["id"])
                if (
                    dup is not None
                    and dup["status"] == "failed"
                    and retry_failed is not None
                    and retry_failed(dup)
                ):
                    conn.execute(
                        "UPDATE jobs SET idem_key=NULL, idem_expires_at=NULL WHERE id=?",
                        (dup["id"],),
                    )
                    dup = None
            else:
                dup = _find_implicit(
                    conn, spec.client_id, sha, now, cfg.queue.implicit_dedup_window_s
                )
            if dup is not None:
                bodies.discard(job_id)
                return SubmitResult(dup["id"], False, JobStatus(dup["status"]))
            conn.execute(
                "INSERT INTO jobs(id, client_id, idem_key, idem_expires_at, body_sha256,"
                " created_at, priority, prio_rank, task_type, model_alias, policy, data_class,"
                " pool, deadline_at, max_queue_wait_s, timeout_s, status, route, route_reason,"
                " est_tokens_in, est_tokens_out, req_path, max_attempts, est_cost_usd)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, spec.client_id, spec.idem_key,
                 now + cfg.queue.idem_ttl_s if spec.idem_key is not None else None, sha, now,
                 spec.priority, spec.priority.rank, spec.task_type, spec.model_alias,
                 spec.policy, spec.data_class, spec.pool, spec.deadline_at,
                 spec.max_queue_wait_s, spec.timeout_s, JobStatus.QUEUED, spec.route,
                 spec.route_reason, spec.est_tokens_in, out, str(path),
                 cfg.retry.job_max_attempts, spec.est_cost_usd),
            )  # fmt: skip
            if spec.pool is not None:
                leases.create_implicit(conn, job_id, spec.pool, spec.client_id, now)
    except BaseException:
        bodies.discard(job_id)
        raise
    return SubmitResult(job_id=job_id, created=True, status=JobStatus.QUEUED)


def pool_queue(conn: sqlite3.Connection, pool: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM jobs WHERE pool=? AND status='queued' ORDER BY prio_rank, created_at",
        (pool,),
    ).fetchall()


def pick_next(conn: sqlite3.Connection, pool: str, now: float) -> sqlite3.Row | None:
    """Priority rank, then round-robin by client (oldest last start first), then created_at."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT j.* FROM jobs j WHERE j.pool=? AND j.status='queued' ORDER BY j.prio_rank,"
        " (SELECT MAX(k.started_at) FROM jobs k WHERE k.pool=j.pool AND k.client_id=j.client_id)"
        " IS NOT NULL,"
        " (SELECT MAX(k.started_at) FROM jobs k WHERE k.pool=j.pool AND k.client_id=j.client_id),"
        " j.created_at LIMIT 1",
        (pool,),
    ).fetchone()
    return row


def mark_running(
    conn: sqlite3.Connection, job_id: str, instance_id: str, worker_id: str, now: float
) -> bool:
    cur = conn.execute(
        "UPDATE jobs SET status='running', instance_id=?, worker_id=?, worker_heartbeat_at=?,"
        " started_at=?, attempts=attempts+1 WHERE id=? AND status='queued'",
        (instance_id, worker_id, now, now, job_id),
    )
    return cur.rowcount == 1


def heartbeat(conn: sqlite3.Connection, job_ids: list[str], worker_id: str, now: float) -> None:
    for job_id in job_ids:
        conn.execute(
            "UPDATE jobs SET worker_heartbeat_at=? WHERE id=? AND status='running' AND worker_id=?",
            (now, job_id, worker_id),
        )


def complete(
    conn: sqlite3.Connection,
    job_id: str,
    result: JobRunResult,
    cost_usd: float,
    now: float,
    res_path: str | None = None,
) -> None:
    """Mark completed and close the implicit lease in the same transaction.

    `res_path` is an S0 addition (optional trailing argument)."""
    conn.execute(
        "UPDATE jobs SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'completed' END,"
        " tokens_in=?, tokens_out=?, finish_reason=?,"
        " cost_usd=?, finished_at=?, res_path=? WHERE id=?",
        (result.tokens_in, result.tokens_out, result.finish_reason, cost_usd, now, res_path,
         job_id),
    )  # fmt: skip
    leases.close_for_job(conn, job_id, now)


def _close_lease(conn: sqlite3.Connection, job_id: str, now: float, reason: str) -> None:
    conn.execute(
        "UPDATE leases SET closed_at=?, close_reason=?"
        " WHERE job_id=? AND kind='implicit' AND closed_at IS NULL",
        (now, reason, job_id),
    )


_CLEAR_RUN = "instance_id=NULL, worker_id=NULL, worker_heartbeat_at=NULL"
MAX_RESTARTS = 5  # L5: a job restarted more often than this goes to local fallback


def requeue(
    conn: sqlite3.Connection,
    job_id: str,
    now: float,
    *,
    count_attempt: bool,
    reason: str,
    close_lease: bool = False,
) -> Literal["queued", "fallback", "failed"]:
    """Put a running/queued job back into the queue.

    `mark_running` already did attempts+1. `count_attempt=False` refunds it (a controller
    restart is not an attempt). With the attempts exhausted the job goes to local fallback.
    "failed" is returned (and nothing changed) when the job is already terminal.
    `close_lease=True` closes the implicit lease so a rejected job does not pin the GPU.
    """
    with _join_or_open(conn):
        row = conn.execute(
            "SELECT status, attempts, max_attempts, cancel_requested, restarts, pool FROM jobs"
            " WHERE id=?",
            (job_id,),
        ).fetchone()
        if row is None or row["status"] not in ("running", "queued"):
            return "failed"
        # R1-10: the run is over; its job reservation must not stay open until E20
        reservations.release_ref(conn, "job", job_id, now, f"requeue:{reason}")
        if row["cancel_requested"]:
            _finish_cancel(conn, job_id, now)
            return "failed"
        if int(row["restarts"]) > MAX_RESTARTS:
            record_incident(
                conn, now, "job_max_restarts", "high", ref=job_id,
                dedup_key=f"job_max_restarts:{job_id}",
                details={"restarts": int(row["restarts"]), "reason": reason},
            )  # fmt: skip
            if row["pool"] is None:  # local job: a fallback would loop forever
                local.fail(conn, job_id, "local_unavailable", now, reason="max_restarts")
                return "failed"
            fallback_local(conn, job_id, "max_restarts", now)
            return "fallback"
        attempts = row["attempts"] if count_attempt else max(row["attempts"] - 1, 0)
        if attempts >= row["max_attempts"]:
            fallback_local(conn, job_id, f"attempts_exhausted:{reason}", now)
            return "fallback"
        conn.execute(
            f"UPDATE jobs SET status='queued', attempts=?, {_CLEAR_RUN}, error_code=? WHERE id=?",
            (attempts, reason, job_id),
        )
        if close_lease:
            _close_lease(conn, job_id, now, reason)
        return "queued"


def fallback_local(
    conn: sqlite3.Connection, job_id: str, reason: UnavailableReason | str, now: float
) -> None:
    """Send the job to the local queue (route 'local:fallback'); closes its implicit lease
    and releases its job reservation."""
    with _join_or_open(conn):
        reservations.release_ref(conn, "job", job_id, now, "fallback_local")
        conn.execute(
            f"UPDATE jobs SET route='local:fallback', pool=NULL, status='queued',"
            f" fallback_reason=?, {_CLEAR_RUN} WHERE id=? AND status IN ('queued','running')",
            (str(reason), job_id),
        )
        _close_lease(conn, job_id, now, "fallback_local")


def _finish_cancel(conn: sqlite3.Connection, job_id: str, now: float) -> None:
    conn.execute(
        f"UPDATE jobs SET status='cancelled', finished_at=?, {_CLEAR_RUN} WHERE id=?",
        (now, job_id),
    )
    _close_lease(conn, job_id, now, "cancelled")


def cancel(conn: sqlite3.Connection, job_id: str, now: float) -> JobStatus:
    """Queued -> cancelled at once; running -> `cancel_requested` (worker stops it);
    terminal jobs are returned unchanged."""
    with _join_or_open(conn):
        row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        status = JobStatus(row["status"])
        if status in (JobStatus.QUEUED, JobStatus.SUBMITTED):
            _finish_cancel(conn, job_id, now)
            return JobStatus.CANCELLED
        if status is JobStatus.RUNNING:
            conn.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
        return status


def requeue_orphaned_running(
    conn: sqlite3.Connection, boot_id: str, now: float, stale_s: float
) -> list[str]:
    """Running jobs of an earlier boot (worker_id != boot_id) or with a stale heartbeat go
    back to the queue. A restart is NOT an attempt: attempts refunded, `restarts` += 1."""
    with _join_or_open(conn):
        rows = conn.execute(
            "SELECT id FROM jobs WHERE status='running' AND (worker_id IS NULL OR worker_id!=?"
            " OR worker_heartbeat_at IS NULL OR worker_heartbeat_at<?) ORDER BY created_at",
            (boot_id, now - stale_s),
        ).fetchall()
        ids = [r["id"] for r in rows]
        for job_id in ids:
            conn.execute("UPDATE jobs SET restarts=restarts+1 WHERE id=?", (job_id,))
            requeue(conn, job_id, now, count_attempt=False, reason="restart")
        return ids


RESULT_TTL_S = 604800.0


def purge_expired(conn: sqlite3.Connection, now: float) -> None:
    """Free idempotency keys and delete result files older than 7 days."""
    with _join_or_open(conn):
        _release_expired_keys(conn, now)
        old = conn.execute(
            "SELECT id, res_path FROM jobs WHERE res_path IS NOT NULL AND finished_at IS NOT NULL"
            " AND finished_at<=?",
            (now - RESULT_TTL_S,),
        ).fetchall()
        for r in old:
            Path(r["res_path"]).unlink(missing_ok=True)
            conn.execute("UPDATE jobs SET res_path=NULL WHERE id=?", (r["id"],))
