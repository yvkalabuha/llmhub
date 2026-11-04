"""Deterministic test harness: FakeClock + MockProvider + MockInference + Controller.

S0 version with the final public surface; stream F extends it (arrive_at, run_until, ...).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from llmhub.clock import FakeClock
from llmhub.configuration.models import Config, PoolConfig
from llmhub.controller import Controller
from llmhub.persistence.db import connect, migrate
from llmhub.provider_adapters.mock import MockKnobs, MockProvider
from llmhub.provider_adapters.mock_inference import MockInference, MockInferenceKnobs
from llmhub.provider_adapters.mock_store import MockStore
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


def make_job(n: int = 0, *, priority: Priority = Priority.NORMAL, pool: str = "p1") -> JobSpec:
    return JobSpec(
        client_id=CLIENT_ID, body=f"request-{n}".encode(), priority=priority, model_alias="m1",
        policy="BALANCED", data_class="private", route="cloud", pool=pool, route_reason="test",
        est_tokens_in=1200, max_tokens=780,
    )  # fmt: skip


class Harness:
    def __init__(
        self,
        data_dir: Path,
        cfg: Config | None = None,
        knobs: MockKnobs | None = None,
        inference_knobs: MockInferenceKnobs | None = None,
    ) -> None:
        self.cfg = cfg or default_config(data_dir)
        self.clock = FakeClock()
        self.conn = connect(data_dir / "state.db", busy_timeout_ms=self.cfg.db.busy_timeout_ms)
        migrate(self.conn)
        self.conn.execute(
            "INSERT INTO clients(id, name, key_hash, created_at) VALUES (?,?,?,?)",
            (CLIENT_ID, "client-1", "x", self.clock.now()),
        )
        self.provider = MockProvider(MockStore(":memory:"), self.clock, knobs)
        self.inference = MockInference(self.provider, self.clock, inference_knobs)
        self.controller = Controller(
            self.conn, self.cfg, self.clock, {"mock": self.provider}, {"mock": self.inference},
            BodyStore(data_dir / "bodies"), TEST_SECRET, boot_id="boot-1",
        )  # fmt: skip
        self._arrivals: list[tuple[float, JobSpec]] = []

    def arrive_at(self, t: float, spec: JobSpec) -> None:
        self._arrivals.append((t, spec))
        self._arrivals.sort(key=lambda a: a[0])

    def submit(self, spec: JobSpec) -> SubmitResult:
        return self.controller.submit(spec)

    async def _settle(self) -> None:
        for _ in range(5):
            await asyncio.sleep(0)

    async def run_until(self, t: float, max_iterations: int = 100_000) -> None:
        """Advance the FakeClock event by event up to `t`, running controller steps."""
        for _ in range(max_iterations):
            now = self.clock.now()
            while self._arrivals and self._arrivals[0][0] <= now:
                self.controller.submit(self._arrivals.pop(0)[1])
            nw = await self.controller.step()
            await self._settle()
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

    def transitions(self, instance_id: str | None = None) -> list[tuple[str, str, str]]:
        sql = "SELECT from_state, to_state, reason FROM transitions"
        params: tuple[str, ...] = ()
        if instance_id is not None:
            sql += " WHERE instance_id=?"
            params = (instance_id,)
        rows = self.conn.execute(sql + " ORDER BY id", params).fetchall()
        return [(r["from_state"], r["to_state"], r["reason"]) for r in rows]
