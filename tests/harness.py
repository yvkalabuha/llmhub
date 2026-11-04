"""Deterministic test harness: FakeClock + MockProvider + MockInference + Controller (stream F).

- `run_until(t)` advances the FakeClock event by event: arrivals, `next_wakeup` of the
  controller, clock waiters (job runs, hanging creates, retry backoff). A `step()` that is
  itself blocked on the clock (retry backoff) is driven by advancing to the next waiter.
- background tasks (create/destroy/jobs) are settled until the event loop has nothing ready;
  a finished background task makes the harness step again at the same time.
- `drive(coro)` runs an admin coroutine (kill, reaper pass) while advancing the clock.
- `restart()` simulates a controller crash: its tasks are cancelled, a new Controller with a
  new boot id is built on the same state.db and the same "cloud".
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from llmhub.clock import FakeClock
from llmhub.configuration.models import Config, PoolConfig
from llmhub.controller import Controller
from llmhub.persistence.db import connect, migrate
from llmhub.provider_adapters.mock import MockKnobs, MockProvider
from llmhub.provider_adapters.mock_inference import MockInference, MockInferenceKnobs
from llmhub.provider_adapters.mock_store import MockStore
from llmhub.reaper.core import Reaper
from llmhub.task_queue.bodies import BodyStore
from llmhub.task_queue.models import JobSpec, Priority, SubmitResult

TEST_SECRET = b"test-secret-0123456789abcdef-test-secret"  # fictitious, >= 32 bytes
CLIENT_ID = "client-1"


def default_config(data_dir: Path, **pool_overrides: object) -> Config:
    pool = PoolConfig(
        name="p1", provider="mock", gpu="RTX4090", vram_gb=24, pp_tps=1200, tg_tps=78,
        image="llmhub-infer:test", disk_gb=50, models=["m1"], **pool_overrides,  # type: ignore[arg-type]
    )  # fmt: skip
    return Config(data_dir=data_dir, pools={"p1": pool})


def make_job(
    n: int = 0,
    *,
    priority: Priority = Priority.NORMAL,
    pool: str = "p1",
    client: str = CLIENT_ID,
    **kw: Any,
) -> JobSpec:
    fields: dict[str, Any] = {"est_tokens_in": 1200, "max_tokens": 780} | kw
    return JobSpec(
        client_id=client, body=f"request-{n}".encode(), priority=priority, model_alias="m1",
        policy="BALANCED", data_class="private", route="cloud", pool=pool, route_reason="test",
        **fields,
    )  # fmt: skip


def _ready_count() -> int:
    loop = asyncio.get_running_loop()
    return len(getattr(loop, "_ready", ()))


class TrackingExecutor(ThreadPoolExecutor):
    """Default executor that counts in-flight `asyncio.to_thread` work: a loop with an empty
    ready queue is not quiet while a worker thread (JSON parse, secret scan) still runs.

    `pending` drops in the asyncio future's done callback (R3-5), i.e. on the loop after the
    result is delivered: the awaiting task's wakeup is then already in the ready queue. The
    concurrent future's callback would fire earlier, before asyncio schedules the result."""

    def __init__(self) -> None:
        super().__init__(max_workers=4, thread_name_prefix="test-exec")
        self.pending = 0
        self._lock = threading.Lock()

    def track(self, fut: asyncio.Future[Any]) -> None:
        with self._lock:
            self.pending += 1
        fut.add_done_callback(self._done)

    def _done(self, _fut: asyncio.Future[Any]) -> None:
        with self._lock:
            self.pending -= 1


def install_tracking_executor() -> None:
    """Call from a running loop (async fixture/test) before driving a FakeClock."""
    loop = asyncio.get_running_loop()
    if isinstance(getattr(loop, "_default_executor", None), TrackingExecutor):
        return
    ex = TrackingExecutor()
    loop.set_default_executor(ex)
    orig = loop.run_in_executor

    def run_in_executor(executor: Any, func: Any, *args: Any) -> asyncio.Future[Any]:
        fut = orig(executor, func, *args)
        if executor is None or executor is ex:
            ex.track(fut)
        return fut

    loop.run_in_executor = run_in_executor  # type: ignore[method-assign]


def threads_busy() -> bool:
    ex = getattr(asyncio.get_running_loop(), "_default_executor", None)
    return isinstance(ex, TrackingExecutor) and ex.pending > 0


class Harness:
    def __init__(
        self,
        data_dir: Path,
        cfg: Config | None = None,
        knobs: MockKnobs | None = None,
        inference_knobs: MockInferenceKnobs | None = None,
        *,
        clients: tuple[str, ...] = (CLIENT_ID,),
        store_path: Path | str = ":memory:",
        **controller_kw: Any,
    ) -> None:
        self.data_dir = data_dir
        self.cfg = cfg or default_config(data_dir)
        self.clock = FakeClock()
        self.db_path = data_dir / "state.db"
        self.conn = connect(self.db_path, busy_timeout_ms=self.cfg.db.busy_timeout_ms)
        migrate(self.conn)
        for c in clients:
            self.add_client(c)
        self.provider = MockProvider(MockStore(store_path), self.clock, knobs)
        self.inference = MockInference(self.provider, self.clock, inference_knobs)
        self.bodies = BodyStore(data_dir / "bodies")
        self.controller_kw = controller_kw
        self._boots = 0
        self.controller = self._make_controller()
        self._arrivals: list[tuple[float, JobSpec]] = []
        self.results: dict[int, SubmitResult] = {}
        self._n = 0

    def add_client(self, client_id: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO clients(id, name, key_hash, created_at, data_class)"
            " VALUES (?,?,?,?,'cloud_allowed')",
            (client_id, client_id, "x", self.clock.now() if hasattr(self, "clock") else 0.0),
        )

    def _make_controller(self) -> Controller:
        self._boots += 1
        return Controller(
            self.conn, self.cfg, self.clock, {"mock": self.provider}, {"mock": self.inference},
            self.bodies, TEST_SECRET, boot_id=f"boot-{self._boots}", **self.controller_kw,
        )  # fmt: skip

    def restart(self) -> Controller:
        """Controller crash + restart: tasks of the old process die, state survives."""
        old = self.controller
        for task in [*old._jobs.values(), *old._ops.values()]:
            task.cancel()
        self.controller = self._make_controller()
        return self.controller

    def reaper(self, **kw: Any) -> Reaper:
        conn = connect(self.db_path, busy_timeout_ms=self.cfg.db.reaper_busy_timeout_ms)
        return Reaper(conn, self.cfg, {"mock": self.provider}, self.clock, TEST_SECRET, **kw)

    # --- arrivals ----------------------------------------------------------
    def arrive_at(self, t: float, spec: JobSpec) -> None:
        self._arrivals.append((t, spec))
        self._arrivals.sort(key=lambda a: a[0])

    def submit(self, spec: JobSpec) -> SubmitResult:
        return self.controller.submit(spec)

    def _submit_due(self) -> None:
        now = self.clock.now()
        while self._arrivals and self._arrivals[0][0] <= now:
            self._n += 1
            self.results[self._n] = self.controller.submit(self._arrivals.pop(0)[1])

    # --- time --------------------------------------------------------------
    async def _settle(self) -> None:
        """Let background tasks run until the event loop has nothing ready."""
        quiet = 0
        for _ in range(2000):
            if threads_busy():  # real time: let the worker thread finish
                await asyncio.sleep(0.001)
                quiet = 0
                continue
            await asyncio.sleep(0)
            quiet = quiet + 1 if _ready_count() == 0 else 0
            if quiet >= 2:
                return

    def _advance(self, limit: float | None = None) -> bool:
        w = self.clock.next_waiter()
        if self._arrivals and (w is None or self._arrivals[0][0] < w):
            w = self._arrivals[0][0]
        if w is None or (limit is not None and w > limit):
            return False
        if w > self.clock.now():
            self.clock.advance_to(w)
        else:
            self.clock.advance_to(self.clock.now())
        return True

    async def drive[T](self, aw: Awaitable[T], limit: float | None = None) -> T:
        """Await `aw` while advancing the clock to the next waiter whenever it blocks."""
        task: asyncio.Future[T] = asyncio.ensure_future(aw)
        for _ in range(100_000):
            await self._settle()
            if task.done():
                return task.result()
            self._submit_due()
            if not self._advance(limit):
                await self._settle()
                if task.done():
                    return task.result()
                raise AssertionError("coroutine blocked with nothing to advance to")
            await self._settle()
        raise AssertionError("drive did not finish")

    async def step(self) -> float:
        self.controller.clear_wake()
        return await self.drive(self.controller.step())

    async def run_until(self, t: float, max_iterations: int = 200_000) -> None:
        """Advance the FakeClock event by event up to `t`, running controller steps."""
        same_t, last_t = 0, None
        for _ in range(max_iterations):
            same_t = same_t + 1 if self.clock.now() == last_t else 0
            last_t = self.clock.now()
            if same_t > 2000:
                raise AssertionError(f"controller loops at t={last_t}: {self.transitions()[-3:]}")
            self._submit_due()
            nw = await self.step()
            await self._settle()
            if self.controller.wake_pending():
                continue
            now = self.clock.now()
            if now >= t and nw > now:
                return
            candidates = [t, nw]
            waiter = self.clock.next_waiter()
            if waiter is not None:
                candidates.append(waiter)
            if self._arrivals:
                candidates.append(self._arrivals[0][0])
            nxt = min(candidates)
            if nxt > now:
                self.clock.advance_to(nxt)
                await self._settle()
        raise AssertionError("harness did not converge")

    async def run_for(self, dt: float) -> None:
        await self.run_until(self.clock.now() + dt)

    # --- inspection ----------------------------------------------------------
    def transitions(self, instance_id: str | None = None) -> list[tuple[str, str, str]]:
        sql = "SELECT from_state, to_state, reason FROM transitions"
        params: tuple[str, ...] = ()
        if instance_id is not None:
            sql += " WHERE instance_id=?"
            params = (instance_id,)
        rows = self.conn.execute(sql + " ORDER BY id", params).fetchall()
        return [(r["from_state"], r["to_state"], r["reason"]) for r in rows]

    def instances(self) -> list[Any]:
        return self.conn.execute("SELECT * FROM instances ORDER BY created_at, id").fetchall()

    def jobs(self) -> list[Any]:
        return self.conn.execute("SELECT * FROM jobs ORDER BY created_at, id").fetchall()

    def incidents(self, kind: str | None = None) -> list[Any]:
        if kind is None:
            return self.conn.execute("SELECT * FROM incidents ORDER BY id").fetchall()
        return self.conn.execute(
            "SELECT * FROM incidents WHERE kind=? ORDER BY id", (kind,)
        ).fetchall()

    def state_of(self) -> str | None:
        rows = self.instances()
        return None if not rows else str(rows[-1]["state"])

    def ledger_total(self, source: str = "estimate") -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount_usd), 0) FROM ledger WHERE source=?", (source,)
        ).fetchone()
        return float(row[0])
