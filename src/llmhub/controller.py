"""Controller: deterministic `step()` over DB state (plan sections 2.4, 3.9).

S0 walking skeleton: one pass per pool, at most one transition per instance per step, happy
path only. KILL-file checks, reconcile, timeouts, budget, leases beyond implicit, create
retries and error recovery are added by streams C-F.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from collections.abc import Mapping

from llmhub.budget_manager.limits import Gate
from llmhub.clock import Clock
from llmhub.cloud_manager.inference import (
    InferenceBackend,
    InstanceLost,
    JobRunSpec,
    NetworkLost,
)
from llmhub.cloud_manager.pools import PoolRuntime
from llmhub.cloud_manager.provisioner import provision_step
from llmhub.configuration.models import Config
from llmhub.killswitch import KillReport
from llmhub.lifecycle_manager import leases, teardown
from llmhub.lifecycle_manager.machine import (
    insert_provisioning,
    live_instance,
    transition,
    try_idle_to_stopping,
)
from llmhub.lifecycle_manager.recovery import ReconcileReport, reconcile_on_start
from llmhub.lifecycle_manager.states import State
from llmhub.persistence.db import Busy, tx
from llmhub.provider_adapters.base import ProviderAdapter
from llmhub.provider_adapters.labels import make_label, new_op_id
from llmhub.task_queue import queue
from llmhub.task_queue.bodies import BodyStore
from llmhub.task_queue.models import JobSpec, SubmitResult

POLL_INTERVAL_S = 5.0  # polling cadence for BOOTING / LOADING_MODEL / teardown


def _allow(conn: sqlite3.Connection) -> Gate:
    return Gate(ok=True)  # S0: budget / HALTED gate arrives with stream E


class Controller:
    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: Config,
        clock: Clock,
        adapters: Mapping[str, ProviderAdapter],
        inference: Mapping[str, InferenceBackend],
        bodies: BodyStore,
        secret: bytes,
        boot_id: str,
    ) -> None:
        self.conn = conn
        self.cfg = cfg
        self.clock = clock
        self.adapters = adapters
        self.inference = inference
        self.bodies = bodies
        self.secret = secret
        self.boot_id = boot_id
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._wake = asyncio.Event()

    async def start(self) -> ReconcileReport:
        return await reconcile_on_start(
            self.conn, self.adapters, self.inference, self.secret, self.clock.now(),
            self.cfg, self.boot_id,
        )  # fmt: skip

    def submit(self, spec: JobSpec) -> SubmitResult:
        result = queue.submit(self.conn, self.bodies, spec, self.clock.now(), self.cfg)
        self._wake.set()
        return result

    # --- step -------------------------------------------------------------
    async def step(self) -> float:
        """One pass; returns next_wakeup (== now when progress was made)."""
        now = self.clock.now()
        wake = float("inf")
        try:
            for name in self.cfg.pools:
                wake = min(wake, await self._step_pool(name, now))
        except Busy:
            return now + POLL_INTERVAL_S  # retry on the next step
        return wake

    def _pool_rt(self, name: str) -> PoolRuntime:
        pool = self.cfg.pools[name]
        return PoolRuntime(pool, self.adapters[pool.provider], self.inference[pool.provider])

    async def _step_pool(self, name: str, now: float) -> float:
        rt = self._pool_rt(name)
        wake = float("inf")
        for dying in self.conn.execute(
            "SELECT * FROM instances WHERE pool=? AND state IN ('PENDING_DELETE','LEAKED')",
            (name,),
        ).fetchall():
            if await teardown.confirm_zero(
                self.conn, rt.adapter, dying, now, self.cfg, "controller"
            ):
                wake = now
            else:
                wake = min(wake, now + POLL_INTERVAL_S)
        inst = live_instance(self.conn, name)
        if inst is None:
            return min(wake, self._maybe_launch(rt, now))
        state = State(inst["state"])
        if state in (State.PROVISIONING, State.BOOTING, State.LOADING_MODEL):
            await provision_step(self.conn, rt, inst, now, self.cfg, self.secret)
            after = live_instance(self.conn, name)
            return now if after is None or after["state"] != state else now + POLL_INTERVAL_S
        if state == State.READY:
            return self._dispatch(rt, inst, now, State.READY, "E8")
        if state == State.BUSY:
            return self._step_busy(rt, inst, now)
        if state == State.IDLE:
            return self._step_idle(rt, inst, now)
        if state == State.STOPPING:
            await teardown.request_destroy(self.conn, rt.adapter, inst, now, self.cfg, "controller")
            return now
        return now + POLL_INTERVAL_S

    def _maybe_launch(self, rt: PoolRuntime, now: float) -> float:
        # a pool with no live instance may still hold a PENDING_DELETE one: teardown below
        if queue.pool_queue(self.conn, rt.cfg.name):
            op_id = new_op_id()
            iid = insert_provisioning(
                self.conn, pool=rt.cfg.name, provider=rt.cfg.provider, op_id=op_id,
                label=make_label(op_id, self.secret),
                max_lifetime_at=now + self.cfg.timeouts.max_lifetime_s, now=now, gate=_allow,
            )  # fmt: skip
            if iid is not None:
                return now
        return float("inf")

    def _running_count(self, instance_id: str) -> int:
        return int(
            self.conn.execute(
                "SELECT count(*) FROM jobs WHERE instance_id=? AND status='running'",
                (instance_id,),
            ).fetchone()[0]
        )

    def _dispatch(
        self, rt: PoolRuntime, inst: sqlite3.Row, now: float, from_state: State, edge: str
    ) -> float:
        """Hand out one job if capacity allows; READY with nothing to do -> IDLE (E9)."""
        pool = rt.cfg.name
        job: sqlite3.Row | None = None
        progressed = False
        with tx(self.conn):
            if self._running_count(inst["id"]) < rt.cfg.max_concurrency:
                job = queue.pick_next(self.conn, pool, now)
            if job is not None:
                queue.mark_running(self.conn, job["id"], inst["id"], self.boot_id, now)
                if from_state != State.BUSY:
                    transition(self.conn, inst["id"], from_state, State.BUSY, edge, now,
                               "controller", idle_since=None)  # fmt: skip
                progressed = True
            elif from_state == State.READY and leases.live_count(self.conn, pool, now) == 0:
                transition(self.conn, inst["id"], State.READY, State.IDLE, "E9", now,
                           "controller", idle_since=now)  # fmt: skip
                progressed = True
        if job is not None:
            self._spawn(rt, inst, job)
        if progressed:
            return now
        return now + POLL_INTERVAL_S if from_state == State.READY else float("inf")

    def _step_busy(self, rt: PoolRuntime, inst: sqlite3.Row, now: float) -> float:
        if self._running_count(inst["id"]) < rt.cfg.max_concurrency and queue.pool_queue(
            self.conn, rt.cfg.name
        ):
            return self._dispatch(rt, inst, now, State.BUSY, "E11")
        if self._running_count(inst["id"]) == 0 and not leases.live_count(
            self.conn, rt.cfg.name, now
        ):
            with tx(self.conn):
                transition(self.conn, inst["id"], State.BUSY, State.IDLE, "E12", now,
                           "controller", idle_since=now)  # fmt: skip
            return now
        return float("inf")  # woken by job completion

    def _step_idle(self, rt: PoolRuntime, inst: sqlite3.Row, now: float) -> float:
        if queue.pool_queue(self.conn, rt.cfg.name):
            return self._dispatch(rt, inst, now, State.IDLE, "E13")
        due = float(inst["idle_since"]) + rt.cfg.idle_timeout_s
        if now >= due:
            try_idle_to_stopping(self.conn, inst["id"], now, "idle_timeout")
            return now
        return due

    # --- job execution ------------------------------------------------------
    def _spawn(self, rt: PoolRuntime, inst: sqlite3.Row, job: sqlite3.Row) -> None:
        task = asyncio.get_running_loop().create_task(self._run_job(rt, inst, job))
        self._tasks[job["id"]] = task
        task.add_done_callback(lambda _t, jid=job["id"]: self._tasks.pop(jid, None))

    async def _run_job(self, rt: PoolRuntime, inst: sqlite3.Row, job: sqlite3.Row) -> None:
        started = self.clock.now()
        spec = JobRunSpec(
            job_id=job["id"], model_alias=job["model_alias"], tokens_in=job["est_tokens_in"],
            max_tokens=job["est_tokens_out"], body=self.bodies.read_request(job["id"]),
        )  # fmt: skip
        try:
            result = await rt.inference.run(inst["endpoint"], spec)
        except (InstanceLost, NetworkLost):
            return  # stream C/F: E17 requeue path
        path = self.bodies.write_result(job["id"], result.body)
        now = self.clock.now()
        price = float(inst["price_usd_h"] or 0.0)
        with tx(self.conn):
            queue.complete(self.conn, job["id"], result, price / 3600 * (now - started),
                           now, str(path))  # fmt: skip
        self._wake.set()

    # --- loop / admin ---------------------------------------------------------
    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            nw = await self.step()
            self._wake.clear()
            sleeper = asyncio.ensure_future(self.clock.sleep_until(nw))
            waker = asyncio.ensure_future(self._wake.wait())
            stopper = asyncio.ensure_future(stop.wait())
            _, pending = await asyncio.wait(
                {sleeper, waker, stopper}, return_when=asyncio.FIRST_COMPLETED
            )
            for p in pending:
                p.cancel()

    async def kill(self, reason: str = "admin") -> KillReport:
        raise NotImplementedError("stream F (F7)")

    def resume(self, provider: str | None = None) -> None:
        raise NotImplementedError("stream E/F")

    def hold(self, pool: str, ttl_s: float, holder: str = "admin") -> str:
        raise NotImplementedError("stream C/F")

    def release(self, pool: str) -> None:
        raise NotImplementedError("stream C/F")


def new_boot_id() -> str:
    return uuid.uuid4().hex[:12]
