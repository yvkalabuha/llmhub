"""T-369 stream I fixtures: the real composition root (`build_service`) on a FakeClock.

Not `i_fixtures.py`: the `i_*` prefix belongs to T-370 (infer image). Everything is real except
the outside world: MockProvider/MockInference stand in for the cloud, `FakeLlama` (ASGI) for
the local llama-servers. The app's own lifespan runs the controller, local worker, waiter and
loop-lag loops; `pump` only moves the FakeClock from one waiter to the next.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import httpx

from api_fixtures import ADMIN_KEY, GAME_KEY, api_config
from fake_llama import FakeLlama
from harness import TEST_SECRET
from llmhub.api_gateway.app import create_app
from llmhub.clock import FakeClock
from llmhub.configuration.models import Config
from llmhub.provider_adapters.mock import MockKnobs, MockProvider
from llmhub.provider_adapters.mock_inference import MockInference, MockInferenceKnobs
from llmhub.provider_adapters.mock_store import MockStore
from llmhub.server import Service, build_service

ENV = {"LLMHUB_LABEL_SECRET": TEST_SECRET.decode()}

__all__ = ["ADMIN_KEY", "ENV", "GAME_KEY", "IntEnv", "api_config"]


def _ready() -> int:
    return len(getattr(asyncio.get_running_loop(), "_ready", ()))


async def settle() -> None:
    quiet = 0
    for _ in range(5000):
        await asyncio.sleep(0)
        quiet = quiet + 1 if _ready() == 0 else 0
        if quiet >= 3:
            return


class IntEnv:
    def __init__(
        self,
        tmp: Path,
        cfg: Config | None = None,
        knobs: MockKnobs | None = None,
        inference_knobs: MockInferenceKnobs | None = None,
    ) -> None:
        self.tmp = tmp
        self.cfg = cfg or api_config(tmp)
        self.clock = FakeClock()
        self.knobs = knobs or MockKnobs()
        self.iknobs = inference_knobs or MockInferenceKnobs()
        self.fakes = {n: FakeLlama(self.clock) for n in self.cfg.local_backends}
        self.boots = 0
        self._lifespan: contextlib.AbstractAsyncContextManager[Any] | None = None

    # --- process lifecycle ------------------------------------------------------------
    async def start(self) -> IntEnv:
        self.boots += 1
        self.provider = MockProvider(MockStore(self.tmp / "mock_cloud.db"), self.clock, self.knobs)
        self.inference = MockInference(self.provider, self.clock, self.iknobs)
        self.svc: Service = build_service(
            self.cfg, ENV, self.clock,
            adapters={"mock": self.provider}, inference={"mock": self.inference},
            local_http={n: f.client() for n, f in self.fakes.items()},
            boot_id=f"boot-{self.boots}",
        )  # fmt: skip
        self.rt = self.svc.rt
        self.conn = self.rt.conn
        self.app = create_app(self.rt, run_loops=True)
        self._lifespan = self.app.router.lifespan_context(self.app)
        await self._lifespan.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
            base_url="http://t",
        )
        await settle()
        return self

    async def stop(self) -> None:
        """Graceful shutdown (lifespan exit + Service.aclose)."""
        await self.client.aclose()
        if self._lifespan is not None:
            await self._lifespan.__aexit__(None, None, None)
            self._lifespan = None
        await self.svc.aclose()
        self.conn.close()

    async def crash(self) -> None:
        """Process death: in-flight cloud job/op tasks die with it, rows stay as they are."""
        c = self.rt.controller
        for t in [*c._jobs.values(), *c._ops.values()]:
            t.cancel()
        await settle()
        await self.stop()

    async def restart(self) -> IntEnv:
        await self.crash()
        return await self.start()

    # --- time -------------------------------------------------------------------------
    async def pump(self, seconds: float) -> None:
        """Advance the FakeClock by `seconds`, waiter by waiter, letting every loop run."""
        end = self.clock.now() + seconds
        for _ in range(1_000_000):
            await settle()
            nxt = self.clock.next_waiter()
            if nxt is None or nxt > end:
                self.clock.advance_to(max(end, self.clock.now()))
                await settle()
                return
            self.clock.advance_to(max(nxt, self.clock.now()))
        raise AssertionError("pump did not converge")

    async def pump_until(self, cond: Any, limit_s: float = 3600, slice_s: float = 5) -> None:
        spent = 0.0
        while not cond():
            if spent >= limit_s:
                raise AssertionError("condition not reached")
            await self.pump(slice_s)
            spent += slice_s

    # --- HTTP -------------------------------------------------------------------------
    async def call(
        self, method: str, url: str, *, key: str | None = GAME_KEY, pump_s: float = 0, **kw: Any
    ) -> httpx.Response:
        headers = dict(kw.pop("headers", {}))
        if key is not None:
            headers.setdefault("Authorization", f"Bearer {key}")
        body = kw.pop("json", None)
        if body is not None:
            kw["content"] = json.dumps(body)
            headers["Content-Type"] = "application/json"
        task = asyncio.ensure_future(self.client.request(method, url, headers=headers, **kw))
        await settle()
        if pump_s:
            # drive the clock until the answer arrives (the sync window ends by itself);
            # `pump_s` is only the expected time, the hard limit keeps a broken test finite
            spent = 0.0
            while not task.done():
                if spent > max(pump_s, self.cfg.api.sync_wait_s + 60):
                    task.cancel()
                    raise AssertionError(f"{method} {url}: no answer after {spent:g} s")
                await self.pump(5.0)
                spent += 5.0
        return await asyncio.wait_for(task, 30)  # real-time guard against a hang

    async def post(self, url: str, body: Any = None, **kw: Any) -> httpx.Response:
        return await self.call("POST", url, json=body, **kw)

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return await self.call("GET", url, **kw)

    def job(self, job_id: str) -> Any:
        return self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def jobs(self, where: str = "1=1") -> list[Any]:
        return self.conn.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY created_at").fetchall()
