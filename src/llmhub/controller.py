"""Controller: deterministic `step()` over DB state (plan sections 2.4, 3.9; stream F).

One pass of `step()`:
  KILL file (start) -> start-up reconcile (once) -> heartbeats -> leases -> orphaned jobs ->
  state timeouts -> liveness -> E3 fallback / ERROR -> STOPPING -> leaks -> duplicate sweep ->
  per pool (teardown, provisioning, dispatch, idle, launch) -> accrual -> budget -> provider
  reconcile -> daily backup -> KILL file (end).
Provider calls never run inside a transaction; `create` and every destroy run as background
asyncio tasks whose intent is written to the DB first, so a hanging call blocks neither
`step()` nor a kill. All timers live in the DB; `step()` returns the next wake-up time.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from collections.abc import Coroutine, Mapping
from typing import Any

from llmhub.budget_manager import limits, reservations
from llmhub.budget_manager.allocation import allocate_session
from llmhub.budget_manager.estimate import JobLoad, estimate_launch, get_speeds, t_cold_p90
from llmhub.budget_manager.limits import Gate, HoldExceedsSessionBudget, Stage
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
from llmhub.cost_tracker import ledger
from llmhub.cost_tracker.reconcile import (
    ensure_resume_allowed,
    reconcile,
    reconcile_due,
    reload_billing,
)
from llmhub.killswitch import KillReport, begin_kill, check_kill_file, kill_all, kill_pass
from llmhub.lifecycle_manager import leases, machine, teardown, timeouts
from llmhub.lifecycle_manager.leases import LeaseKind
from llmhub.lifecycle_manager.machine import insert_provisioning, live_instance, transition
from llmhub.lifecycle_manager.recovery import (
    ReconcileReport,
    destroy_duplicates,
    reconcile_on_start,
)
from llmhub.lifecycle_manager.safety import (
    delete_key,
    get_key,
    record_incident,
    resume_provider,
    set_key,
)
from llmhub.lifecycle_manager.states import LIVE_IN_INDEX, State
from llmhub.persistence import state as cstate
from llmhub.persistence.backup import maybe_daily_backup
from llmhub.persistence.db import Busy, tx
from llmhub.provider_adapters.base import ProviderAdapter, UnavailableReason
from llmhub.provider_adapters.guarded import CircuitBreaker, GuardedAdapter
from llmhub.provider_adapters.labels import make_label, new_op_id
from llmhub.scheduler.triggers import launch_trigger, load_queued, work_seconds
from llmhub.task_queue import queue
from llmhub.task_queue.bodies import BodyStore
from llmhub.task_queue.models import JobSpec, Priority, SubmitResult

POLL_INTERVAL_S = 5.0  # polling cadence for PROVISIONING..LOADING_MODEL and teardown
ALIVE_CHECK_S = 30.0  # provider/endpoint liveness of READY/BUSY/IDLE instances
DUP_SWEEP_S = 60.0  # duplicate-resource sweep (section 3.9)
PURGE_S = 3600.0
ACCRUE_S = 60.0  # ledger accrual granularity (keeps the ledger small; reservations cover it)
BUDGET_CHECK_S = 30.0  # stage / warnings / 100 % actions
MAX_FAILED_CYCLES = 3  # consecutive failed launch cycles of a pool before local fallback
INF = float("inf")


class KillFilePresent(Exception):
    """resume() refused while the KILL file exists."""


class HoldRejected(Exception):
    def __init__(self, gate: Gate) -> None:
        super().__init__(gate.detail)
        self.gate = gate


def _reason_of(last_error: str | None, create_called: bool) -> UnavailableReason:
    if last_error in tuple(UnavailableReason):
        return UnavailableReason(str(last_error))
    if not create_called:
        return UnavailableReason.GPU_UNAVAILABLE
    return UnavailableReason.DEPLOYMENT_ERROR


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
        *,
        guard: bool = True,
        poll_s: float = POLL_INTERVAL_S,
        alive_check_s: float = ALIVE_CHECK_S,
        dup_sweep_s: float = DUP_SWEEP_S,
    ) -> None:
        self.conn = conn
        self.cfg = cfg
        self.clock = clock
        self.raw_adapters = dict(adapters)
        self.adapters: dict[str, ProviderAdapter] = {
            name: self._guard(a) if guard else a for name, a in adapters.items()
        }
        self.inference = inference
        self.bodies = bodies
        self.secret = secret
        self.boot_id = boot_id
        self.poll_s = poll_s
        self.alive_check_s = alive_check_s
        self.dup_sweep_s = dup_sweep_s
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._ops: dict[str, asyncio.Task[Any]] = {}
        self._wake = asyncio.Event()
        self._started = False
        self._alive_at: dict[str, float] = {}
        self._dup_at: dict[str, float] = {}
        self._purge_at = -INF
        self._accrue_at = -INF
        self._budget_at = -INF
        self._no_launch = False
        self._op_last: dict[str, tuple[float, str]] = {}  # teardown op key -> (when, state)

    def _guard(self, adapter: ProviderAdapter) -> ProviderAdapter:
        if isinstance(adapter, GuardedAdapter):
            return adapter
        breaker = CircuitBreaker(self.conn, adapter.name, self.clock, self.cfg.retry)
        return GuardedAdapter(adapter, self.clock, self.cfg.retry, breaker, actor="controller")

    # --- start / submit -----------------------------------------------------
    async def start(self) -> ReconcileReport:
        """Restart reconciliation (section 8); runs once, before the first step."""
        now = self.clock.now()
        report = await reconcile_on_start(
            self.conn, self.adapters, self.inference, self.secret, now, self.cfg, self.boot_id
        )
        from llmhub.reaper.core import orphan_sweep  # reaper imports controller-free modules

        await orphan_sweep(self.conn, self.adapters, self.secret, now, self.cfg, "controller")
        with tx(self.conn):
            report.requeued_jobs += queue.requeue_orphaned_running(
                self.conn, self.boot_id, now, self.cfg.timeouts.worker_stale_s
            )
            queue.purge_expired(self.conn, now)
        if cstate.get(self.conn, "safe_mode") == "1":
            self._restore_bodies(now)
        self._started = True
        return report

    def _restore_bodies(self, now: float) -> None:
        """Safe mode: jobs whose request body survived but whose row was lost are re-queued."""
        for job_id, meta, body in list(self.bodies.iter_recoverable()):
            if self.conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
                continue
            client = meta.get("client_id")
            if not client or "model_alias" not in meta:
                continue
            with tx(self.conn):
                self.conn.execute(
                    "INSERT OR IGNORE INTO clients(id, name, key_hash, created_at)"
                    " VALUES (?,?,?,?)",
                    (client, client, "restored", now),
                )
            fields = {k: v for k, v in meta.items() if k in JobSpec.__dataclass_fields__}
            fields["priority"] = Priority(fields.get("priority", "normal"))
            spec = JobSpec(body=body, **fields)
            queue.submit(self.conn, self.bodies, spec, now, self.cfg)
            self.bodies.discard(job_id)

    def submit(self, spec: JobSpec) -> SubmitResult:
        result = queue.submit(self.conn, self.bodies, spec, self.clock.now(), self.cfg)
        self._wake.set()
        return result

    def wake_pending(self) -> bool:
        """True when a background task finished since the last step (step again now)."""
        return self._wake.is_set()

    def clear_wake(self) -> None:
        self._wake.clear()

    def busy_ops(self) -> int:
        return sum(1 for t in self._ops.values() if not t.done())

    # --- step -------------------------------------------------------------
    async def step(self) -> float:
        """One pass; returns next_wakeup (== now when progress was made)."""
        now = self.clock.now()
        try:
            if not self._started:
                await self.start()
            return await self._step(now)
        except Busy:
            return now + self.poll_s  # the action is retried on the next step

    async def _step(self, now: float) -> float:
        wake = INF
        if check_kill_file(self.cfg.data_dir):
            wake = min(wake, await self._kill_file_pass(now))
        self._heartbeats(now)
        with tx(self.conn):
            leases.expire_due(self.conn, now)
            self._release_closed_hold_reservations(now)
            queue.requeue_orphaned_running(
                self.conn, self.boot_id, now, self.cfg.timeouts.worker_stale_s
            )
        if now - self._purge_at >= PURGE_S:
            queue.purge_expired(self.conn, now)
            self._purge_at = now
        timeouts.apply_timeouts(self.conn, now, self.cfg)
        await self._check_alive(now)
        self._before_error_to_stopping(now)
        machine.error_to_stopping(self.conn, now)
        teardown.mark_leaks(self.conn, now, self.cfg, "controller")
        await self._duplicate_sweep(now)
        if now - self._budget_at >= BUDGET_CHECK_S:
            self._no_launch = limits.stage(self.conn, now, self.cfg) == Stage.NO_LAUNCH
        for name in self.cfg.pools:
            wake = min(wake, await self._step_pool(name, now, self._no_launch))
        if now - self._accrue_at >= ACCRUE_S:  # E20/E22 finalize to the exact second
            ledger.accrue(self.conn, now)
            self._accrue_at = now
        if now - self._budget_at >= BUDGET_CHECK_S:
            self._budget(now, self._no_launch)
            self._budget_at = now
        await self._reconcile(now)
        maybe_daily_backup(self.conn, self.cfg.data_dir / "backups", self.clock)
        if check_kill_file(self.cfg.data_dir):
            wake = min(wake, await self._kill_file_pass(now))
        if self.busy_ops():
            wake = min(wake, now + self.poll_s)
        if self._jobs:
            wake = min(wake, now + self.cfg.timeouts.worker_heartbeat_s)
        return max(wake, now)

    # --- step parts -------------------------------------------------------
    async def _kill_file_pass(self, now: float) -> float:
        begin_kill(self.conn, now, "controller", "kill_file")
        remaining = await kill_pass(
            self.conn, self.adapters, self.secret, now, self.cfg, "controller"
        )
        return now + 2.0 if remaining else INF

    def _heartbeats(self, now: float) -> None:
        with tx(self.conn):
            set_key(self.conn, "heartbeat", repr(now), now)
            if self._jobs:
                queue.heartbeat(self.conn, list(self._jobs), self.boot_id, now)

    def _release_closed_hold_reservations(self, now: float) -> None:
        self.conn.execute(
            "UPDATE reservations SET released_at=?, release_reason='lease_closed'"
            " WHERE scope IN ('hold','prewarm') AND released_at IS NULL AND ref_id IN"
            " (SELECT id FROM leases WHERE closed_at IS NOT NULL OR expires_at<=?)",
            (now, now),
        )

    async def _check_alive(self, now: float) -> None:
        rows = self.conn.execute(
            "SELECT * FROM instances WHERE provider_ref IS NOT NULL AND state IN"
            " ('BOOTING','LOADING_MODEL','READY','BUSY','IDLE') ORDER BY id"
        ).fetchall()
        for row in rows:
            if now - self._alive_at.get(row["id"], -INF) < self.alive_check_s:
                continue
            self._alive_at[row["id"]] = now
            await timeouts.check_alive(
                self.conn, self.adapters[row["provider"]], self.inference[row["provider"]],
                row, now, self.cfg,
            )  # fmt: skip

    def _before_error_to_stopping(self, now: float) -> None:
        """E3 sends the pool queue to local fallback (section 6.8); count failed cycles."""
        for row in self.conn.execute("SELECT * FROM instances WHERE state='ERROR'").fetchall():
            last = self.conn.execute(
                "SELECT from_state FROM transitions WHERE instance_id=? AND to_state='ERROR'"
                " ORDER BY id DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            src = None if last is None else last["from_state"]
            if src not in ("PROVISIONING", "BOOTING", "LOADING_MODEL"):
                continue
            key = f"failed_cycles:{row['pool']}"
            with tx(self.conn):
                n = int(get_key(self.conn, key) or 0) + 1
                set_key(self.conn, key, str(n), now)
                if src == "PROVISIONING":
                    reason = _reason_of(row["last_error"], row["create_called_at"] is not None)
                    self._fallback_pool(row["pool"], reason, now)

    def _fallback_pool(self, pool: str, reason: UnavailableReason | str, now: float) -> int:
        rows = self.conn.execute(
            "SELECT id FROM jobs WHERE pool=? AND status='queued' ORDER BY created_at", (pool,)
        ).fetchall()
        for r in rows:
            queue.fallback_local(self.conn, r["id"], reason, now)
        return len(rows)

    async def _duplicate_sweep(self, now: float) -> None:
        """Live rows: resources with the row's label but another ref are destroyed (3.9)."""
        for name, adapter in self.adapters.items():
            if now - self._dup_at.get(name, -INF) < self.dup_sweep_s:
                continue
            marks = ",".join("?" for _ in LIVE_IN_INDEX)
            rows = self.conn.execute(
                f"SELECT * FROM instances WHERE provider=? AND provider_ref IS NOT NULL"
                f" AND state IN ({marks})",
                (name, *sorted(LIVE_IN_INDEX)),
            ).fetchall()
            if not rows:
                continue
            self._dup_at[name] = now
            try:
                owned = await adapter.list_owned("llmhub-")
            except Exception:  # noqa: BLE001, S112 - adapters may raise anything; next sweep
                continue
            refs = {r.ref for r in owned}
            for row in rows:
                if row["provider_ref"] not in refs:
                    continue  # ghost / not yet visible: the provisioner decides
                extras = [
                    r for r in owned if r.label == row["label"] and r.ref != row["provider_ref"]
                ]
                if extras:
                    await destroy_duplicates(self.conn, adapter, row, extras, now)

    def _aligned_poll(self, since: float, now: float) -> float:
        """Next poll at since + k * poll_s: the cadence does not drift with unrelated steps."""
        k = int((now - since) // self.poll_s) + 1
        return since + k * self.poll_s

    def _pool_rt(self, name: str) -> PoolRuntime:
        pool = self.cfg.pools[name]
        return PoolRuntime(pool, self.adapters[pool.provider], self.inference[pool.provider])

    def _spawn_op(self, key: str, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._ops[key] = task

        def done(t: asyncio.Task[Any], k: str = key) -> None:
            if self._ops.get(k) is t:
                del self._ops[k]
            if not t.cancelled() and t.exception() is not None:
                try:
                    record_incident(
                        self.conn, self.clock.now(), "background_op_failed", "warning", ref=k,
                        details={"error": type(t.exception()).__name__},
                    )  # fmt: skip
                except Busy:
                    pass  # the op is retried by the next step anyway
            self._wake.set()

        task.add_done_callback(done)

    def _op_running(self, key: str) -> bool:
        t = self._ops.get(key)
        return t is not None and not t.done()

    def _launch_jobs(self, iid: str) -> frozenset[str]:
        raw = get_key(self.conn, f"launch_jobs:{iid}")
        return frozenset(json.loads(raw)) if raw else frozenset()

    def _on_stopped(self, conn: sqlite3.Connection, row: sqlite3.Row, now: float) -> None:
        """E20/E22 hook (inside the final transaction): finalize ledger, release, allocate."""
        delete_key(conn, f"launch_jobs:{row['id']}")
        ledger.finalize(conn, row["id"], float(row["zero_confirmed_at"] or now))
        reservations.release_instance(conn, row["id"], now, "instance_stopped")
        allocate_session(conn, row["id"])

    async def _step_pool(self, name: str, now: float, no_launch: bool) -> float:
        rt = self._pool_rt(name)
        wake = INF
        for row in self.conn.execute(
            "SELECT * FROM instances WHERE pool=? AND state IN ('STOPPING','PENDING_DELETE',"
            "'LEAKED') ORDER BY created_at",
            (name,),
        ).fetchall():
            key = f"teardown:{row['id']}"
            last = self._op_last.get(key)
            due = last is None or last[1] != row["state"] or now - last[0] >= self.poll_s or (
                row["next_destroy_at"] is not None and now >= float(row["next_destroy_at"])
                and last[0] < float(row["next_destroy_at"])
            )
            if due and not self._op_running(key):
                self._op_last[key] = (now, row["state"])
                if row["state"] == State.STOPPING:
                    self._spawn_op(key, teardown.request_destroy(
                        self.conn, rt.adapter, row, now, self.cfg, "controller"))  # fmt: skip
                else:
                    self._spawn_op(key, teardown.confirm_zero(
                        self.conn, rt.adapter, row, now, self.cfg, "controller",
                        on_stopped=self._on_stopped))  # fmt: skip
            nxt = row["next_destroy_at"]
            wake = min(wake, now + self.poll_s if nxt is None else max(float(nxt), now + 1.0))
        inst = live_instance(self.conn, name)
        if inst is None:
            return min(wake, self._maybe_launch(rt, now))
        state = State(inst["state"])
        if state in (State.PROVISIONING, State.BOOTING, State.LOADING_MODEL):
            if not self._op_running(f"create:{inst['id']}"):
                await provision_step(
                    self.conn, rt, inst, now, self.cfg, self.secret,
                    spawn=lambda iid, c: self._spawn_op(f"create:{iid}", c),
                    now_fn=self.clock.now,
                )  # fmt: skip
            after = self.conn.execute(
                "SELECT state, state_since FROM instances WHERE id=?", (inst["id"],)
            ).fetchone()
            if after["state"] != state:
                return now
            return min(wake, self._aligned_poll(float(after["state_since"]), now))
        if state in (State.READY, State.BUSY, State.IDLE):
            if get_key(self.conn, f"failed_cycles:{name}") not in (None, "0"):
                with tx(self.conn):
                    set_key(self.conn, f"failed_cycles:{name}", "0", now)
            return min(wake, self._serve(rt, inst, now, no_launch))
        return min(wake, now + self.poll_s)  # ERROR is moved to STOPPING by the next step

    # --- serving a live instance -------------------------------------------------
    def _serve(self, rt: PoolRuntime, inst: sqlite3.Row, now: float, no_launch: bool) -> float:
        iid = inst["id"]
        if inst["stop_after_current"] and inst["state"] in (State.READY, State.IDLE):
            with tx(self.conn):
                ok = transition(self.conn, iid, State(inst["state"]), State.STOPPING, "E15", now,
                                "controller", stop_reason=inst["stop_reason"],
                                reason=inst["stop_reason"] or "stop_after_current")  # fmt: skip
            return now if ok else now + self.poll_s
        progressed = False
        if not inst["stop_after_current"] and inst["net_fail_since"] is None:
            progressed = self._dispatch(rt, iid, now)
        outcome = machine.settle_idle(self.conn, iid, now, self.cfg, no_launch=no_launch)
        if outcome == "stopping" or progressed:
            return now
        row = self.conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
        wake = now + self.alive_check_s
        if row["state"] == State.IDLE:
            wake = min(wake, machine.idle_due_at(row, self.cfg))
        elif float(row["max_lifetime_at"]) > now:
            wake = min(wake, float(row["max_lifetime_at"]))
        exp = self.conn.execute(
            "SELECT MIN(expires_at) FROM leases WHERE pool=? AND closed_at IS NULL"
            " AND expires_at > ?",
            (rt.cfg.name, now),
        ).fetchone()[0]
        if exp is not None:
            wake = min(wake, float(exp))
        return max(wake, now)

    def _dispatch(self, rt: PoolRuntime, iid: str, now: float) -> bool:
        """Hand out queued jobs while capacity allows (E8 / E13 / E11)."""
        progressed = False
        while True:
            spawned: sqlite3.Row | None = None
            with tx(self.conn):
                inst = self.conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
                state = State(inst["state"])
                if state not in (State.READY, State.BUSY, State.IDLE) or inst["stop_after_current"]:
                    break
                if machine.running_count(self.conn, iid) >= rt.cfg.max_concurrency:
                    break
                job = queue.pick_next(self.conn, rt.cfg.name, now)
                if job is None:
                    break
                load = JobLoad(job["id"], int(job["est_tokens_in"]), int(job["est_tokens_out"]))
                if job["id"] in self._launch_jobs(iid):
                    gate = Gate(ok=True)  # already inside the session reservation
                else:
                    gate = limits.reserve_job_on_running(self.conn, iid, load, now, self.cfg)
                if not gate.ok:
                    progressed = True
                    if gate.detail == "session":  # session budget: finish, then a new cycle
                        machine.request_stop_after_current(self.conn, iid, "budget_session")
                        break
                    # a rejected job must not pin the GPU: local fallback closes its lease
                    queue.fallback_local(
                        self.conn, job["id"], gate.reason or "budget_limit_reached", now
                    )
                    continue
                queue.mark_running(self.conn, job["id"], iid, self.boot_id, now)
                edge = {State.READY: "E8", State.IDLE: "E13", State.BUSY: "E11"}[state]
                transition(self.conn, iid, state, State.BUSY, edge, now, "controller",
                           idle_since=None)  # fmt: skip
                spawned = job
            progressed = True
            self._spawn_job(self._pool_rt(rt.cfg.name), inst, spawned)
        return progressed

    # --- launching ----------------------------------------------------------------
    def _pool_price(self, name: str) -> tuple[float, float]:
        row = self.conn.execute(
            "SELECT price_usd_h, offer_json FROM instances WHERE pool=? AND price_usd_h IS NOT"
            " NULL ORDER BY created_at DESC LIMIT 1",
            (name,),
        ).fetchone()
        if row is None:
            return self.cfg.pools[name].max_price_usd_h, 0.0
        disk = json.loads(row["offer_json"]).get("disk_usd_h", 0.0) if row["offer_json"] else 0.0
        return float(row["price_usd_h"]), float(disk)

    def _maybe_launch(self, rt: PoolRuntime, now: float) -> float:
        pool = rt.cfg
        jobs = load_queued(self.conn, pool.name)
        prewarm = leases.live_count(self.conn, pool.name, now, [LeaseKind.PREWARM]) > 0
        if not jobs and not prewarm:
            return INF
        t_cold = t_cold_p90(self.conn, pool.name, pool.cold_start_s_default)
        decision = launch_trigger(
            jobs, now, self.cfg.queue, t_cold, lambda j: work_seconds(j, pool), prewarm
        )
        if not decision.launch:
            return decision.wake_at if decision.wake_at is not None else INF
        if int(get_key(self.conn, f"failed_cycles:{pool.name}") or 0) >= MAX_FAILED_CYCLES:
            with tx(self.conn):
                self._fallback_pool(pool.name, UnavailableReason.DEPLOYMENT_ERROR, now)
                set_key(self.conn, f"failed_cycles:{pool.name}", "0", now)
            return now
        price, disk = self._pool_price(pool.name)
        pp, tg = get_speeds(self.conn, pool.name, pool.pp_tps, pool.tg_tps)
        loads = [JobLoad(j.job_id, j.est_tokens_in, j.est_tokens_out) for j in jobs]
        est = estimate_launch(
            price, loads, t_cold, pool.idle_timeout_s, pp, tg, disk_usd_h=disk
        )
        cap = self.cfg.budget.budget_session
        while len(loads) > 1 and est.usd > cap:  # one session takes what fits, the rest waits
            loads = loads[:-1]
            est = estimate_launch(
                price, loads, t_cold, pool.idle_timeout_s, pp, tg, disk_usd_h=disk
            )
        over = [jid for jid, usd in est.per_job.items() if usd > self.cfg.budget.budget_job]
        if over:  # too expensive for the cloud on its own: local, the rest launches next pass
            with tx(self.conn):
                for jid in over:
                    queue.fallback_local(self.conn, jid, UnavailableReason.BUDGET_LIMIT_REACHED, now)
            return now
        priority = min((j.priority for j in jobs), key=lambda p: p.rank, default=Priority.NORMAL)
        op_id = new_op_id()
        inner = limits.launch_gate(est, pool.provider, now, self.cfg, priority, pool.subbudget,
                                   op_id)  # fmt: skip
        seen: list[Gate] = []

        def gate(conn: sqlite3.Connection) -> Gate:
            g = inner(conn)
            seen.append(g)
            if g.ok:  # these jobs are paid by the session reservation (no 2nd job reserve)
                set_key(conn, f"launch_jobs:inst-{op_id}",
                        json.dumps([ld.job_id for ld in loads]), now)  # fmt: skip
            return g

        iid = insert_provisioning(
            self.conn, pool=pool.name, provider=pool.provider, op_id=op_id,
            label=make_label(op_id, self.secret),
            max_lifetime_at=now + self.cfg.timeouts.max_lifetime_s, now=now, gate=gate,
        )  # fmt: skip
        if iid is not None:
            return now
        if seen and not seen[-1].ok:
            with tx(self.conn):
                self._fallback_pool(pool.name, seen[-1].reason or "launch_refused", now)
            return now
        return now + self.poll_s  # UNIQUE race: another cycle is live

    # --- budget / reconcile -------------------------------------------------------
    def _budget(self, now: float, no_launch: bool) -> None:
        with tx(self.conn):
            if no_launch:
                for r in self.conn.execute(
                    "SELECT id FROM instances WHERE state='BUSY' AND stop_after_current=0"
                ).fetchall():
                    machine.request_stop_after_current(self.conn, r["id"], "budget_100")
                self.conn.execute(
                    "UPDATE leases SET closed_at=?, close_reason='budget_100'"
                    " WHERE kind IN ('hold','prewarm') AND closed_at IS NULL",
                    (now,),
                )
                limits.close_hold_prewarm_if_exhausted(self.conn, now, self.cfg)
            limits.warnings(self.conn, now, self.cfg)

    async def _reconcile(self, now: float) -> None:
        for name, adapter in self.adapters.items():
            live = self.conn.execute(
                "SELECT 1 FROM instances WHERE provider=? AND state != 'STOPPED' LIMIT 1", (name,)
            ).fetchone()
            if not reconcile_due(self.conn, name, now, live is not None):
                continue
            try:
                await reconcile(self.conn, adapter, now, self.cfg)
            except Exception as exc:  # noqa: BLE001 - adapters may raise anything
                with tx(self.conn):
                    set_key(self.conn, f"last_reconcile_at:{name}", repr(now), now)
                    record_incident(
                        self.conn, now, "reconcile_failed", "warning", ref=name,
                        dedup_key=f"reconcile_failed:{name}:{int(now // 3600)}",
                        details={"error": type(exc).__name__},
                    )  # fmt: skip

    # --- job execution ------------------------------------------------------
    def _spawn_job(self, rt: PoolRuntime, inst: sqlite3.Row, job: sqlite3.Row) -> None:
        task = asyncio.get_running_loop().create_task(self._run_job(rt, inst, job))
        self._jobs[job["id"]] = task

        def done(_t: asyncio.Task[None], jid: str = job["id"]) -> None:
            self._jobs.pop(jid, None)
            self._wake.set()

        task.add_done_callback(done)

    def _still_mine(self, job_id: str, iid: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM jobs WHERE id=? AND status='running' AND instance_id=? AND worker_id=?",
            (job_id, iid, self.boot_id),
        ).fetchone()
        return row is not None

    async def _run_job(self, rt: PoolRuntime, inst: sqlite3.Row, job: sqlite3.Row) -> None:
        iid = inst["id"]
        started = self.clock.now()
        spec = JobRunSpec(
            job_id=job["id"], model_alias=job["model_alias"], tokens_in=job["est_tokens_in"],
            max_tokens=job["est_tokens_out"], body=self.bodies.read_request(job["id"]),
        )  # fmt: skip
        endpoint = inst["endpoint"] or ""
        try:
            result = await rt.inference.run(endpoint, spec)
        except InstanceLost:
            now = self.clock.now()
            machine.mark_lost(self.conn, iid, now, "instance_lost", cfg=self.cfg)
            self._requeue_mine(job["id"], iid, now, count_attempt=True, reason="instance_lost")
            return
        except NetworkLost:
            now = self.clock.now()
            with tx(self.conn):
                self.conn.execute(
                    "UPDATE instances SET net_fail_since=COALESCE(net_fail_since, ?) WHERE id=?",
                    (now, iid),
                )
            self._requeue_mine(job["id"], iid, now, count_attempt=False, reason="network_lost")
            return
        except Exception:  # noqa: BLE001 - the backend failed this job
            self._requeue_mine(job["id"], iid, self.clock.now(), count_attempt=True,
                               reason="run_error")  # fmt: skip
            return
        now = self.clock.now()
        path = self.bodies.write_result(job["id"], result.body)
        price = float(inst["price_usd_h"] or 0.0)
        with tx(self.conn):
            if self._still_mine(job["id"], iid):
                queue.complete(self.conn, job["id"], result, price / 3600 * (now - started),
                               now, str(path))  # fmt: skip
            reservations.release_ref(self.conn, "job", job["id"], now, "job_done")

    def _requeue_mine(
        self, job_id: str, iid: str, now: float, *, count_attempt: bool, reason: str
    ) -> None:
        with tx(self.conn):
            if self._still_mine(job_id, iid):
                queue.requeue(self.conn, job_id, now, count_attempt=count_attempt, reason=reason)
            reservations.release_ref(self.conn, "job", job_id, now, reason)

    # --- loop / admin ---------------------------------------------------------
    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self._wake.clear()
            nw = await self.step()
            if self._wake.is_set():
                continue
            sleeper = asyncio.ensure_future(self.clock.sleep_until(nw))
            waker = asyncio.ensure_future(self._wake.wait())
            stopper = asyncio.ensure_future(stop.wait())
            _, pending = await asyncio.wait(
                {sleeper, waker, stopper}, return_when=asyncio.FIRST_COMPLETED
            )
            for p in pending:
                p.cancel()

    async def kill(self, reason: str = "admin", deadline_s: float = 60) -> KillReport:
        """HALT + destroy everything we own within `deadline_s` (section 3.9)."""
        report = await kill_all(
            self.conn, self.adapters, self.secret, self.clock, self.cfg, "admin",
            deadline_s, reason=reason,
        )  # fmt: skip
        self._wake.set()
        return report

    async def reload_billing(self) -> None:
        """Provider billing since month start -> ledger (required before resume after
        DB corruption)."""
        await reload_billing(self.conn, self.adapters, self.clock.now(), self.cfg)

    def resume(self, provider: str | None = None) -> None:
        """Manual resume: lift HALTED and provider blocks. Refused while KILL exists or while
        the billing reconcile after DB corruption is missing (ReconcileRequired)."""
        if check_kill_file(self.cfg.data_dir):
            raise KillFilePresent("remove the KILL file first")
        ensure_resume_allowed(self.conn)
        cstate.unhalt(self.conn, self.clock)
        now = self.clock.now()
        names = [provider] if provider else [
            r["provider"] for r in self.conn.execute(
                "SELECT provider FROM provider_accounts WHERE blocked_reason IS NOT NULL"
            ).fetchall()
        ]  # fmt: skip
        for name in names:
            resume_provider(self.conn, name, now)
        self._wake.set()

    def hold(self, pool: str, ttl_s: float, holder: str = "admin") -> str:
        """Admin hold (<= 8 h). Cost to TTL above budget_session raises
        HoldExceedsSessionBudget unless `leases.hold_may_exceed_session`."""
        now = self.clock.now()
        price, disk = self._pool_price(pool)
        gate = limits.check_hold(self.conn, price, ttl_s, now, self.cfg, disk_usd_h=disk)
        if not gate.ok:
            raise HoldRejected(gate)
        live = live_instance(self.conn, pool)
        with tx(self.conn):
            lease_id = leases.create_hold(
                self.conn, holder=holder, pool=pool, ttl_s=ttl_s, now=now, cfg=self.cfg.leases
            )
            reservations.reserve(
                self.conn, "hold", lease_id, (price + disk) / 3600 * ttl_s, now,
                None if live is None else live["id"],
            )  # fmt: skip
        self._wake.set()
        return lease_id

    def release(self, pool: str) -> None:
        now = self.clock.now()
        with tx(self.conn):
            leases.release_holds(self.conn, pool, now, "released")
            self._release_closed_hold_reservations(now)
        self._wake.set()


def new_boot_id() -> str:
    return uuid.uuid4().hex[:12]


__all__ = [
    "Controller", "HoldExceedsSessionBudget", "HoldRejected", "KillFilePresent", "new_boot_id",
]  # fmt: skip
