"""Pool launch triggers (plan section 3.7 / research 6.7). Pure functions, no I/O."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from llmhub.configuration.models import PoolConfig, QueueClassConfig, QueueConfig
from llmhub.task_queue.models import Priority


@dataclass(frozen=True)
class QueuedJob:
    job_id: str
    client_id: str
    priority: Priority
    created_at: float
    deadline_at: float | None = None
    max_queue_wait_s: float | None = None
    est_tokens_in: int = 0
    est_tokens_out: int = 0


@dataclass(frozen=True)
class TriggerDecision:
    launch: bool
    reason: str
    wake_at: float | None  # earliest future moment a time-based trigger fires


def _class_cfg(qcfg: QueueConfig, prio: Priority) -> QueueClassConfig:
    return getattr(qcfg, prio.value)  # type: ignore[no-any-return]


def effective_deadline(job: QueuedJob, qcfg: QueueConfig) -> float | None:
    """min(deadline_at, created_at + max queue wait); a wait of 0 means unlimited."""
    wait = job.max_queue_wait_s
    if wait is None:
        wait = _class_cfg(qcfg, job.priority).maximum_queue_wait_s
    candidates: list[float] = []
    if job.deadline_at is not None:
        candidates.append(job.deadline_at)
    if wait and wait > 0:
        candidates.append(job.created_at + wait)
    return min(candidates) if candidates else None


def launch_trigger(
    jobs: Sequence[QueuedJob],
    now: float,
    qcfg: QueueConfig,
    t_cold_p90: float,
    work_s: Callable[[QueuedJob], float],
    prewarm: bool,
) -> TriggerDecision:
    """Launch on any of: urgent job, batch size, batch max wait, deadline, live prewarm lease.

    Deadline: effective_deadline - now <= t_cold_p90 [+ work_s(job)] + margin, where the work
    term is included only with `qcfg.deadline_includes_work` (False = simulator parity).
    Otherwise `wake_at` is the nearest future time-based trigger.
    """
    if prewarm:
        return TriggerDecision(True, "prewarm", None)
    if any(j.priority is Priority.URGENT for j in jobs):
        return TriggerDecision(True, "urgent", None)
    wakes: list[float] = []
    for prio in Priority:
        group = [j for j in jobs if j.priority is prio]
        if not group:
            continue
        cc = _class_cfg(qcfg, prio)
        if len(group) >= cc.minimum_batch_size:
            return TriggerDecision(True, f"batch:{prio.value}", None)
        if cc.maximum_batch_wait_s > 0:
            at = min(j.created_at for j in group) + cc.maximum_batch_wait_s
            if at <= now:
                return TriggerDecision(True, f"batch_wait:{prio.value}", None)
            wakes.append(at)
    for job in jobs:
        ed = effective_deadline(job, qcfg)
        if ed is None:
            continue
        lead = t_cold_p90 + qcfg.deadline_margin_s
        if qcfg.deadline_includes_work:
            lead += work_s(job)
        if ed - now <= lead:
            return TriggerDecision(True, "deadline", None)
        wakes.append(ed - lead)
    return TriggerDecision(False, "waiting" if jobs else "empty", min(wakes) if wakes else None)


def dispatch_capacity(running: int, max_concurrency: int) -> int:
    return max(0, max_concurrency - running)


def work_seconds(job: QueuedJob, pool: PoolConfig) -> float:
    """T_work = in/pp + out/tg using the pool profile (EWMA values are applied by the caller)."""
    return job.est_tokens_in / pool.pp_tps + job.est_tokens_out / pool.tg_tps


def idle_stop_at(idle_since: float, pool: PoolConfig) -> float:
    """When an IDLE instance of the pool should be stopped (idle_timeout 0..900 s, default 300)."""
    return idle_since + pool.idle_timeout_s


def load_queued(conn: sqlite3.Connection, pool: str) -> list[QueuedJob]:
    rows = conn.execute(
        "SELECT id, client_id, priority, created_at, deadline_at, max_queue_wait_s,"
        " est_tokens_in, est_tokens_out FROM jobs WHERE pool=? AND status='queued'"
        " ORDER BY prio_rank, created_at",
        (pool,),
    ).fetchall()
    return [
        QueuedJob(
            r["id"], r["client_id"], Priority(r["priority"]), r["created_at"], r["deadline_at"],
            r["max_queue_wait_s"], r["est_tokens_in"], r["est_tokens_out"],
        )
        for r in rows
    ]  # fmt: skip
