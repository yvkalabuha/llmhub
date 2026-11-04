"""Stub local worker with the `WorkerLike` surface for gateway tests (no HTTP, no real EWMA)."""

from __future__ import annotations

import json
import sqlite3
import types
from typing import Any

from llmhub.api_gateway.waiter import JobWaiter
from llmhub.clock import Clock
from llmhub.cloud_manager.inference import JobRunResult
from llmhub.configuration.models import Config
from llmhub.task_queue import local
from llmhub.task_queue.bodies import BodyStore

WORKER_ID = "stub-worker"


def completion(text: str = "local answer", finish: str = "stop", **msg: Any) -> bytes:
    return json.dumps(
        {
            "object": "chat.completion", "model": "local-model",
            "choices": [{"index": 0, "finish_reason": finish,
                         "message": {"role": "assistant", "content": text, **msg}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        }
    ).encode()  # fmt: skip


class StubWorker:
    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: Config,
        clock: Clock,
        bodies: BodyStore,
        waiter: JobWaiter,
    ) -> None:
        self.conn, self.cfg, self.clock, self.bodies, self.waiter = conn, cfg, clock, bodies, waiter
        ewma = types.SimpleNamespace(value=1.0)
        self.stats: dict[str, Any] = {
            n: types.SimpleNamespace(available=True, busy_external=False, pp=ewma, tg=ewma)
            for n in cfg.local_backends
        }
        self.eta: float | None = 10.0
        self.is_available = True
        self.hold = False  # True: jobs stay queued (nobody serves them)
        self.hang = False  # True: jobs start but never finish
        self.answer = completion()
        self.kicks = 0
        self.cancelled: list[str] = []

    def kick(self) -> None:
        self.kicks += 1

    def available(self, backend: str) -> bool:
        return self.is_available and backend in self.stats

    def eta_s(
        self, alias: str, tokens_in: int, max_tokens: int, prio_rank: int, now: float
    ) -> float | None:
        return self.eta if self.is_available else None

    def cancel(self, job_id: str) -> bool:
        self.cancelled.append(job_id)
        now = self.clock.now()
        res = JobRunResult(0, 0, "cancelled", b"")
        local.complete_local(self.conn, job_id, WORKER_ID, res, now, "")
        self.waiter.notify(job_id)
        return True

    async def run_forever(self, stop: Any) -> None:  # pragma: no cover - not used with stubs
        await stop.wait()

    def process(self) -> None:
        """Serve every queued local job at once (unless held)."""
        for job_id, _code in local.expire_overdue(self.conn, self.clock.now()):
            self.waiter.notify(job_id)
        if self.hold:
            return
        while (row := local.pick_next_local(self.conn, self.clock.now())) is not None:
            now = self.clock.now()
            name = self.cfg.models[row["model_alias"]].local or "cpu"
            if not local.mark_running_local(self.conn, row["id"], WORKER_ID, f"local:{name}", now):
                continue
            if self.hang:
                return
            path = self.bodies.write_result(row["id"], self.answer)
            res = JobRunResult(
                7, 3, "length" if b'"length"' in self.answer else "stop", self.answer
            )
            local.complete_local(self.conn, row["id"], WORKER_ID, res, now, str(path))
            self.waiter.notify(row["id"])
