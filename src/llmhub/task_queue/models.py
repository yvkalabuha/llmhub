"""Queue types (plan section 3.6)."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Priority(enum.StrEnum):
    URGENT = "urgent"
    NORMAL = "normal"
    LOW = "low"

    @property
    def rank(self) -> int:
        return {"urgent": 0, "normal": 1, "low": 2}[self.value]


class JobStatus(enum.StrEnum):
    SUBMITTED = "submitted"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class JobSpec:
    client_id: str
    body: bytes
    priority: Priority
    model_alias: str
    policy: str
    data_class: str
    route: str
    pool: str | None
    route_reason: str
    task_type: str | None = None
    idem_key: str | None = None
    deadline_at: float | None = None
    max_queue_wait_s: float | None = None
    timeout_s: float | None = None
    est_tokens_in: int = 0
    max_tokens: int | None = None


@dataclass(frozen=True)
class SubmitResult:
    job_id: str
    created: bool
    status: JobStatus


class IdempotencyConflict(Exception):
    def __init__(self, job_id: str) -> None:
        super().__init__(job_id)
        self.job_id = job_id
