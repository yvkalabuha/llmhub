"""Fixtures of stream H: Harness (controller + FakeClock) plus a LocalWorker on ASGI fakes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fake_llama import FakeLlama
from harness import CLIENT_ID, Harness
from llmhub.configuration.api_models import (
    LocalBackendConfig,
    LocalConfig,
    ModelAliasConfig,
    TaskTypeConfig,
)
from llmhub.configuration.models import Config, PoolConfig
from llmhub.local_inference.client import LocalBackendClient
from llmhub.local_inference.worker import LocalWorker
from llmhub.task_queue.models import JobSpec, Priority

FAKE_URLS = {"cpu": "http://fake-llama-a/v1", "cpu2": "http://fake-llama-b/v1"}


def local_config(data_dir: Path, **local_kw: Any) -> Config:
    pool = PoolConfig(
        name="p1", provider="mock", gpu="RTX4090", vram_gb=24, pp_tps=1200, tg_tps=78,
        image="llmhub-infer:test", disk_gb=50, models=["m1"],
    )  # fmt: skip
    backends = {
        n: LocalBackendConfig(url=u, model=f"model-{n}", ctx_tokens=8192, pp_tps=100, tg_tps=5)
        for n, u in FAKE_URLS.items()
    }
    return Config(
        data_dir=data_dir, pools={"p1": pool}, local_backends=backends,
        models={"m1": ModelAliasConfig(local="cpu"), "m2": ModelAliasConfig(local="cpu2"),
                "cloudonly": ModelAliasConfig()},
        task_types={"think": TaskTypeConfig(max_tokens=300, think=True),
                    "plain": TaskTypeConfig(max_tokens=77)},
        local=LocalConfig(**local_kw),
    )  # fmt: skip


def lspec(
    n: int = 0,
    *,
    alias: str = "m1",
    prio: Priority = Priority.NORMAL,
    client: str = CLIENT_ID,
    route: str = "local",
    body: dict[str, Any] | None = None,
    **kw: Any,
) -> JobSpec:
    payload = body if body is not None else {"messages": [{"role": "user", "content": f"q{n}"}]}
    payload = {**payload, "_llmhub": {"data_class": "private", "policy": "BALANCED"}}
    fields: dict[str, Any] = {"est_tokens_in": 100, "max_tokens": 40} | kw
    return JobSpec(
        client_id=client, body=json.dumps(payload, sort_keys=True).encode(), priority=prio,
        model_alias=alias, policy="BALANCED", data_class="private", route=route, pool=None,
        route_reason="test", **fields,
    )  # fmt: skip


class LocalEnv:
    def __init__(self, tmp: Path, **local_kw: Any) -> None:
        self.cfg = local_config(tmp, **local_kw)
        self.h = Harness(tmp, self.cfg)
        self.clock = self.h.clock
        self.conn = self.h.conn
        self.fakes = {n: FakeLlama(self.clock) for n in FAKE_URLS}
        self.notified: list[str] = []
        self.worker = self.make_worker()

    def make_worker(self) -> LocalWorker:
        backends = {
            n: LocalBackendClient(n, c, None, http=self.fakes[n].client(), clock=self.clock)
            for n, c in self.cfg.local_backends.items()
        }
        return LocalWorker(
            self.conn, self.cfg, self.clock, self.h.bodies, backends,
            self.h.controller.boot_id, self.notified.append,
        )  # fmt: skip

    def restart(self) -> LocalWorker:
        """Process crash: the old worker's calls die silently, new boot id, new worker."""
        for t in list(self.worker._tasks.values()):
            t.cancel()
        self.h.restart()
        self.worker = self.make_worker()
        return self.worker

    def submit(self, spec: JobSpec) -> str:
        return self.h.controller.submit(spec).job_id

    def job(self, job_id: str) -> Any:
        return self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    async def run_for(self, dt: float, *, controller: bool = True) -> None:
        """Interleave controller and worker steps while the FakeClock advances."""
        h, clock = self.h, self.clock
        end = clock.now() + dt
        for _ in range(100_000):
            h._submit_due()
            nw = [await h.step()] if controller else []
            nw.append(await h.drive(self.worker.step()))
            await h._settle()
            now = clock.now()
            if now >= end and min(nw) > now:
                return
            cand = [end, *nw]
            w = clock.next_waiter()
            if w is not None:
                cand.append(w)
            nxt = min(cand)
            if nxt > now:
                clock.advance_to(nxt)
                await h._settle()
        raise AssertionError("LocalEnv did not converge")
