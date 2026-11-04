"""Job queue operations. S0 implements the happy path; stream D completes idempotency,
dedup, requeue, cancel, fallback_local and purge_expired."""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from typing import Literal

from llmhub.cloud_manager.inference import JobRunResult
from llmhub.configuration.models import Config
from llmhub.lifecycle_manager import leases
from llmhub.persistence.db import tx
from llmhub.provider_adapters.base import UnavailableReason
from llmhub.task_queue.bodies import BodyStore
from llmhub.task_queue.models import JobSpec, JobStatus, SubmitResult


def submit(
    conn: sqlite3.Connection, bodies: BodyStore, spec: JobSpec, now: float, cfg: Config
) -> SubmitResult:
    """S0: plain insert + implicit lease. No idempotency / dedup yet (stream D)."""
    job_id = uuid.uuid4().hex[:12]
    path = bodies.write_request(job_id, spec.body, {"client_id": spec.client_id})
    out = spec.max_tokens or cfg.task_type_max_tokens.get(
        spec.task_type or "default", cfg.task_type_max_tokens["default"]
    )
    with tx(conn):
        conn.execute(
            "INSERT INTO jobs(id, client_id, idem_key, body_sha256, created_at, priority,"
            " prio_rank, task_type, model_alias, policy, data_class, pool, deadline_at,"
            " max_queue_wait_s, timeout_s, status, route, route_reason, est_tokens_in,"
            " est_tokens_out, req_path) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, spec.client_id, spec.idem_key, hashlib.sha256(spec.body).hexdigest(), now,
             spec.priority, spec.priority.rank, spec.task_type, spec.model_alias, spec.policy,
             spec.data_class, spec.pool, spec.deadline_at, spec.max_queue_wait_s, spec.timeout_s,
             JobStatus.QUEUED, spec.route, spec.route_reason, spec.est_tokens_in, out, str(path)),
        )  # fmt: skip
        if spec.pool is not None:
            leases.create_implicit(conn, job_id, spec.pool, spec.client_id, now)
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
        "UPDATE jobs SET status='completed', tokens_in=?, tokens_out=?, finish_reason=?,"
        " cost_usd=?, finished_at=?, res_path=? WHERE id=?",
        (result.tokens_in, result.tokens_out, result.finish_reason, cost_usd, now, res_path,
         job_id),
    )  # fmt: skip
    leases.close_for_job(conn, job_id, now)


def requeue(
    conn: sqlite3.Connection, job_id: str, now: float, *, count_attempt: bool, reason: str
) -> Literal["queued", "fallback", "failed"]:
    raise NotImplementedError("stream D")


def fallback_local(
    conn: sqlite3.Connection, job_id: str, reason: UnavailableReason | str, now: float
) -> None:
    raise NotImplementedError("stream D")


def cancel(conn: sqlite3.Connection, job_id: str, now: float) -> JobStatus:
    raise NotImplementedError("stream D")


def requeue_orphaned_running(
    conn: sqlite3.Connection, boot_id: str, now: float, stale_s: float
) -> list[str]:
    raise NotImplementedError("stream D")


def purge_expired(conn: sqlite3.Connection, now: float) -> None:
    raise NotImplementedError("stream D")
