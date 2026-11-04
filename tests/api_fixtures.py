"""T-369 stream W fixtures: gateway app on the T-368 harness (FakeClock, MockProvider).

Not `w_fixtures.py`: that name belongs to T-370 (weights). The local worker is a stub with the
`WorkerLike` surface (stream H's real worker is exercised in `test_h_*` and in stream I).
"""

from __future__ import annotations

import asyncio
import json
import types
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from api_gateway_stubs import StubWorker
from harness import Harness
from llmhub.api_gateway.app import Runtime, create_app
from llmhub.api_gateway.auth import ClientRegistry, hash_key
from llmhub.api_gateway.waiter import JobWaiter
from llmhub.configuration.api_models import ClientConfig, LocalBackendConfig, ModelAliasConfig
from llmhub.configuration.models import Config, PoolConfig
from llmhub.model_router.views import CloudState
from llmhub.provider_adapters.mock import MockKnobs
from llmhub.task_queue.models import Priority

GAME_KEY = "lh_test_game_key_000000000000"
PRIV_KEY = "lh_test_priv_key_000000000000"
LOW_KEY = "lh_test_low_key_0000000000000"
ADMIN_KEY = "lh_test_admin_key_00000000000"


def chat_body(text: str = "hello", model: str = "big", **extra: Any) -> dict[str, Any]:
    return {"model": model, "messages": [{"role": "user", "content": text}], **extra}


def auth(key: str = GAME_KEY, **headers: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", **headers}


def api_config(data_dir: Path, **over: Any) -> Config:
    pool = PoolConfig(
        name="p1", provider="mock", gpu="RTX4090", vram_gb=24, pp_tps=1200, tg_tps=78,
        image="llmhub-infer:test", disk_gb=50, models=["big", "cloudonly"],
    )  # fmt: skip
    clients = {
        "game": ClientConfig(
            key_sha256=hash_key(GAME_KEY), allowed_models=["*"], max_priority=Priority.NORMAL,
            default_priority=Priority.NORMAL, cloud_allowed=True,
        ),
        "priv": ClientConfig(
            key_sha256=hash_key(PRIV_KEY), allowed_models=["*"], max_priority=Priority.NORMAL,
            default_priority=Priority.NORMAL, cloud_allowed=False,
        ),
        "low": ClientConfig(
            key_sha256=hash_key(LOW_KEY), allowed_models=["localonly"],
            max_priority=Priority.LOW, default_priority=Priority.LOW, cloud_allowed=True,
        ),
    }  # fmt: skip
    base: dict[str, Any] = {
        "data_dir": data_dir,
        "pools": {"p1": pool},
        "local_backends": {
            "cpu": LocalBackendConfig(
                url="http://llama.invalid/v1",
                model="local-model",
                ctx_tokens=32768,
                pp_tps=50,
                tg_tps=5,
            )
        },
        "models": {
            "big": ModelAliasConfig(local="cpu", quality_tier=2),
            "cloudonly": ModelAliasConfig(),
            "localonly": ModelAliasConfig(local="cpu"),
        },
        "task_types": {"summarize": {"max_tokens": 1000, "think": False}},
        "router": {"auto_alias": "big"},
        "clients": clients,
        "api": {"admin_key_sha256": hash_key(ADMIN_KEY)},
    }
    base.update(over)
    return Config.model_validate(base)


class ApiEnv:
    """Harness + registry + waiter + cloud view + stub worker + ASGI app + httpx client."""

    def __init__(
        self,
        tmp: Path,
        cfg: Config | None = None,
        knobs: MockKnobs | None = None,
        config_path: Path | None = None,
    ) -> None:
        self.cfg = cfg or api_config(tmp)
        self.h = Harness(tmp, self.cfg, knobs, clients=())
        self.clock = self.h.clock
        self.registry = ClientRegistry(self.h.conn, self.cfg, self.clock)
        self.registry.sync()
        self.waiter = JobWaiter(self.h.conn, self.clock, self.cfg.api.wait_poll_s)
        self.worker = StubWorker(self.h.conn, self.cfg, self.clock, self.h.bodies, self.waiter)
        self.cloud = CloudState(self.h.conn, self.cfg, self.clock)
        self.rt = Runtime(
            conn=self.h.conn, cfg=self.cfg, clock=self.clock, controller=self.h.controller,
            worker=self.worker, bodies=self.h.bodies, registry=self.registry, waiter=self.waiter,
            cloud=self.cloud, started_at=self.clock.now(), version="test",
            config_path=config_path,
        )  # fmt: skip
        self.app = create_app(self.rt, run_loops=False)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
            base_url="http://t",
        )
        self._stop = asyncio.Event()
        self._poller = asyncio.ensure_future(self.waiter.run(self._stop))

    async def aclose(self) -> None:
        self._stop.set()
        self._poller.cancel()
        await asyncio.gather(self._poller, return_exceptions=True)
        await self.client.aclose()

    # --- driving time ---------------------------------------------------------
    async def pump(self, seconds: float, slice_s: float = 1.0) -> None:
        """Advance the FakeClock by `seconds`: controller steps, stub worker, waiter poller."""
        end = self.clock.now() + seconds
        while self.clock.now() < end:
            await self.h._settle()
            self.worker.process()
            await self.h.run_until(min(end, self.clock.now() + slice_s))
        await self.h._settle()
        self.worker.process()
        await self.h._settle()

    async def call(
        self,
        method: str,
        url: str,
        *,
        key: str | None = GAME_KEY,
        pump_s: float = 0,
        **kw: Any,
    ) -> httpx.Response:
        """One request; with `pump_s` the clock is driven while it is in flight."""
        headers = dict(kw.pop("headers", {}))
        if key is not None:
            headers.setdefault("Authorization", f"Bearer {key}")
        task = asyncio.ensure_future(self.client.request(method, url, headers=headers, **kw))
        if pump_s:
            await self.pump(pump_s)
        return await task

    async def post(self, url: str, body: Any = None, **kw: Any) -> httpx.Response:
        if body is not None:
            kw["content"] = json.dumps(body)
            kw.setdefault("headers", {})["Content-Type"] = "application/json"
        return await self.call("POST", url, **kw)

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return await self.call("GET", url, **kw)

    def job(self, job_id: str) -> Any:
        return self.h.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def jobs(self, where: str = "1=1") -> list[Any]:
        return self.h.conn.execute(
            f"SELECT * FROM jobs WHERE {where} ORDER BY created_at"
        ).fetchall()


def ns(**kw: Any) -> types.SimpleNamespace:
    return types.SimpleNamespace(**kw)


def merge(base: Mapping[str, Any], **kw: Any) -> dict[str, Any]:
    return {**base, **kw}
