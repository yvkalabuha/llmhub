"""LocalWorker: executes `pool IS NULL` jobs on the local llama-servers (T-369 stream H).

One CPU slot (semaphore 1) for all backends. It shares the controller's `boot_id` as
`worker_id` and refreshes `worker_heartbeat_at` of its running jobs, so the core's
`requeue_orphaned_running` neither takes them while we live nor misses them after a restart
(new boot id). Results are written only while the job is still ours (CAS in `complete_local`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

from llmhub.clock import Clock
from llmhub.cloud_manager.inference import JobRunResult
from llmhub.configuration.models import Config
from llmhub.local_inference.client import LocalBackendClient, LocalError
from llmhub.local_inference.eta import local_eta_s, work_s
from llmhub.local_inference.stats import BackendStats, Ewma
from llmhub.persistence.db import Busy, tx
from llmhub.task_queue import local
from llmhub.task_queue.bodies import BodyStore

log = logging.getLogger("llmhub.local_worker")

_RETRYABLE = ("local_unavailable", "upstream_timeout", "bad_response")


class LocalWorker:
    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: Config,
        clock: Clock,
        bodies: BodyStore,
        backends: Mapping[str, LocalBackendClient],
        boot_id: str,
        notify: Callable[[str], None] = lambda _id: None,
    ) -> None:
        self.conn = conn
        self.cfg = cfg
        self.clock = clock
        self.bodies = bodies
        self.backends = backends
        self.boot_id = boot_id
        self._notify = notify
        a = cfg.local.ewma_alpha
        self.stats: dict[str, BackendStats] = {
            n: BackendStats(Ewma(a, c.pp_tps), Ewma(a, c.tg_tps))
            for n, c in cfg.local_backends.items()
        }
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._task_backend: dict[str, str] = {}
        self._cancelled: set[str] = set()
        self._wake = asyncio.Event()
        self._hb_at = -1e18
        self._probe_at = -1e18

    # --- public ------------------------------------------------------------
    def kick(self) -> None:
        self._wake.set()

    def available(self, backend: str) -> bool:
        st = self.stats.get(backend)
        return st is not None and backend in self.backends and st.available

    def cancel(self, job_id: str) -> bool:
        """Cut the upstream call of a running job; the job ends `cancelled`."""
        task = self._tasks.get(job_id)
        if task is None or task.done():
            return False
        self._cancelled.add(job_id)
        task.cancel()
        return True

    def eta_s(
        self, alias: str, tokens_in: int, max_tokens: int, prio_rank: int, now: float
    ) -> float | None:
        model = self.cfg.models.get(alias)
        name = model.local if model is not None else None
        if name is None or not self.available(name):
            return None
        st = self.stats[name]
        ahead: list[tuple[int, int]] = []
        running_left = 0.0
        for r in local.local_queue_ahead(self.conn, prio_rank, now):
            tin, tout = int(r["est_tokens_in"]), int(r["est_tokens_out"])
            if r["status"] == "running":
                spent = now - float(r["started_at"] or now)
                running_left += max(0.0, work_s(tin, tout, st.pp.value, st.tg.value) - spent)
            else:
                ahead.append((tin, tout))
        return local_eta_s(
            ahead=ahead, running_left_s=running_left, own=(tokens_in, max_tokens),
            stats=st, cfg=self.cfg.local,
        )  # fmt: skip

    async def step(self) -> float:
        """One deterministic pass: expiry, cancels, heartbeat, dispatch. Returns next wakeup."""
        now = self.clock.now()
        for job_id, _code in local.expire_overdue(self.conn, now):
            self._notify(job_id)
        self._reap_tasks()
        self._apply_cancels()
        self._heartbeat(now)
        if len(self._tasks) < self.cfg.local.concurrency:
            self._dispatch(now)
        return now + self.cfg.local.poll_s

    async def probe_all(self) -> None:
        for name, client in self.backends.items():
            result = await client.probe()
            st = self.stats[name]
            ours = any(b == name for b in self._task_backend.values())
            st.on_probe(result, ours, self.cfg.local.unavailable_after_failures)
            st.last_probe_at = self.clock.now()

    async def run_forever(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                self._wake.clear()
                now = self.clock.now()
                try:
                    if now - self._probe_at >= self.cfg.local.probe_interval_s:
                        self._probe_at = now
                        await self.probe_all()
                    nxt = await self.step()
                except Busy:
                    # database locked by another process (reaper): retry on the next tick
                    log.warning("local worker step skipped: database busy")
                    nxt = now + self.cfg.local.poll_s
                except Exception as exc:  # noqa: BLE001 - the loop must survive any step error
                    log.error("local worker step failed: %s", type(exc).__name__)
                    nxt = now + self.cfg.local.poll_s
                nxt = min(nxt, self._probe_at + self.cfg.local.probe_interval_s)
                await self._sleep(nxt, stop)
        finally:
            await self.close()

    async def close(self) -> None:
        """Stop without touching job rows: running jobs are requeued by the next boot."""
        tasks = list(self._tasks.values())
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # --- internals -----------------------------------------------------------
    async def _sleep(self, until: float, stop: asyncio.Event) -> None:
        waiters = [
            asyncio.ensure_future(self.clock.sleep_until(until)),
            asyncio.ensure_future(self._wake.wait()),
            asyncio.ensure_future(stop.wait()),
        ]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    def _reap_tasks(self) -> None:
        for job_id in [j for j, t in self._tasks.items() if t.done()]:
            del self._tasks[job_id]
            self._task_backend.pop(job_id, None)
            self._cancelled.discard(job_id)

    def _apply_cancels(self) -> None:
        if not self._tasks:
            return
        rows = self.conn.execute(
            "SELECT id FROM jobs WHERE status='running' AND pool IS NULL AND worker_id=?"
            " AND cancel_requested=1",
            (self.boot_id,),
        ).fetchall()
        for r in rows:
            self.cancel(r["id"])

    def _heartbeat(self, now: float) -> None:
        if not self._tasks:
            return
        if now - self._hb_at < self.cfg.timeouts.worker_heartbeat_s / 2:
            return
        self._hb_at = now
        with tx(self.conn):
            self.conn.executemany(
                "UPDATE jobs SET worker_heartbeat_at=? WHERE id=? AND status='running'"
                " AND worker_id=?",
                [(now, j, self.boot_id) for j in self._tasks],
            )

    def _dispatch(self, now: float) -> None:
        while True:
            row = local.pick_next_local(self.conn, now)
            if row is None:
                return
            job_id = row["id"]
            alias = row["model_alias"]
            model = self.cfg.models.get(alias)
            name = model.local if model is not None else None
            if name is None or name not in self.backends:
                if row["route"] == "local:fallback":
                    local.fail(self.conn, job_id, "cloud_unavailable", now,
                               reason=row["fallback_reason"])  # fmt: skip
                else:
                    local.fail(self.conn, job_id, "local_unavailable", now)
                self._notify(job_id)
                continue
            with tx(self.conn):
                if not local.mark_running_local(
                    self.conn, job_id, self.boot_id, f"local:{name}", now
                ):
                    continue
                self.conn.execute("UPDATE jobs SET error_code=NULL WHERE id=?", (job_id,))
            self._task_backend[job_id] = name
            self._tasks[job_id] = asyncio.ensure_future(self._run(dict(row), name))
            return

    def _build_request(self, job: dict[str, Any]) -> dict[str, Any]:
        request: dict[str, Any] = json.loads(self.bodies.read_request(job["id"]))
        request.pop("_llmhub", None)
        tt = self.cfg.task_types.get(job["task_type"]) if job["task_type"] else None
        if tt is not None and tt.think is not None:
            kwargs = request.get("chat_template_kwargs")
            if not isinstance(kwargs, dict):
                kwargs = {}
            kwargs.setdefault("enable_thinking", tt.think)
            request["chat_template_kwargs"] = kwargs
        if "max_tokens" not in request and "max_completion_tokens" not in request:
            request["max_tokens"] = (
                tt.max_tokens if tt is not None else max(1, int(job["est_tokens_out"]))
            )
        return request

    def _owned(self, job_id: str) -> bool:
        row = self.conn.execute(
            "SELECT status, worker_id FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        return row is not None and row["status"] == "running" and row["worker_id"] == self.boot_id

    async def _run(self, job: dict[str, Any], name: str) -> None:
        job_id = job["id"]
        client = self.backends[name]
        try:
            request = self._build_request(job)
            timeout_s = float(job["timeout_s"] or client.cfg.timeout_s)
            result = await client.chat(request, timeout_s=timeout_s)
        except asyncio.CancelledError:
            if job_id in self._cancelled:
                self._finish_cancel(job_id)
            raise
        except LocalError as exc:
            self._on_error(job_id, exc.code)
            return
        except (OSError, ValueError) as exc:  # unreadable / broken request body
            log.error("local job %s: cannot build request: %s", job_id, type(exc).__name__)
            if self._owned(job_id):
                with tx(self.conn):
                    local.fail(self.conn, job_id, "local_unavailable", self.clock.now(),
                               reason="request_unreadable")  # fmt: skip
                self._notify(job_id)
            return
        self.stats[name].on_result(result)
        now = self.clock.now()
        if not self._owned(job_id):
            return  # taken over (restart, cancel): the late answer is dropped
        path = self.bodies.write_result(job_id, result.body)  # no await up to the CAS
        run = JobRunResult(result.tokens_in, result.tokens_out, result.finish_reason, result.body)
        if local.complete_local(self.conn, job_id, self.boot_id, run, now, str(path)):
            self._notify(job_id)
            self.kick()

    def _on_error(self, job_id: str, code: str) -> None:
        if not self._owned(job_id):
            return
        now = self.clock.now()
        with tx(self.conn):
            if code == "context_too_large":
                local.fail(self.conn, job_id, code, now)
            else:
                local.requeue_local(self.conn, job_id, now, code if code in _RETRYABLE else
                                    "local_unavailable", max_attempts=self.cfg.local.max_attempts)  # fmt: skip
        self._notify(job_id)
        self.kick()

    def _finish_cancel(self, job_id: str) -> None:
        now = self.clock.now()
        with tx(self.conn):
            cur = self.conn.execute(
                "UPDATE jobs SET status='cancelled', cancel_requested=1, finished_at=?,"
                " instance_id=NULL, worker_id=NULL, worker_heartbeat_at=NULL"
                " WHERE id=? AND status='running' AND worker_id=?",
                (now, job_id, self.boot_id),
            )
            if cur.rowcount == 1:
                self.conn.execute(
                    "UPDATE leases SET closed_at=?, close_reason='cancelled'"
                    " WHERE job_id=? AND kind='implicit' AND closed_at IS NULL",
                    (now, job_id),
                )
        self._notify(job_id)
