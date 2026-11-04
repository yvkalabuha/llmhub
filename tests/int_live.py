"""Live fixtures (T-369 I4, reused by O2/O3 and T-373): the real app on uvicorn, real sockets.

`LiveStack` runs, in its own thread and event loop, the API process built by `build_service`
(SystemClock, MockProvider with fast knobs) and fake llama-servers served by uvicorn on free
127.0.0.1 ports (never 8090-8092). The SQLite connection lives in that thread; tests read
state through `query()`, which runs on the server loop.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from api_fixtures import ADMIN_KEY, GAME_KEY, LOW_KEY, PRIV_KEY, api_config
from fake_llama import FakeLlama
from harness import TEST_SECRET
from llmhub.api_gateway.app import create_app
from llmhub.clock import SystemClock
from llmhub.configuration.api_models import LocalBackendConfig
from llmhub.configuration.models import Config
from llmhub.provider_adapters.mock import MockKnobs, MockProvider
from llmhub.provider_adapters.mock_inference import MockInference, MockInferenceKnobs
from llmhub.provider_adapters.mock_store import MockStore
from llmhub.server import Service, build_service

__all__ = [
    "ADMIN_KEY", "GAME_KEY", "LOW_KEY", "PRIV_KEY", "LiveStack", "RichLlama", "free_port",
    "run_serve_process",
]  # fmt: skip

ENV = {"LLMHUB_LABEL_SECRET": TEST_SECRET.decode()}
FORBIDDEN_PORTS = frozenset({8090, 8091, 8092, 8095})


def free_port() -> int:
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = int(s.getsockname()[1])
        if port not in FORBIDDEN_PORTS:
            return port


class RichLlama(FakeLlama):
    """FakeLlama that can also answer with `tool_calls` and `reasoning_content`."""

    def __init__(self) -> None:
        super().__init__(SystemClock())
        self.tool_calls: list[dict[str, Any]] | None = None
        self.reasoning: str | None = None

    def _chat_body(self, req: dict[str, Any]) -> dict[str, Any]:
        body = super()._chat_body(req)
        msg = body["choices"][0]["message"]
        if self.reasoning is not None:
            msg["reasoning_content"] = self.reasoning
        if self.tool_calls is not None and req.get("tools"):
            msg["content"] = None
            msg["tool_calls"] = self.tool_calls
            body["choices"][0]["finish_reason"] = "tool_calls"
        return body


def live_config(data_dir: Path, llama_port: int, **over: Any) -> Config:
    backend = LocalBackendConfig(
        url=f"http://127.0.0.1:{llama_port}/v1", model="local-model", ctx_tokens=32768,
        pp_tps=50, tg_tps=5,
    )  # fmt: skip
    base: dict[str, Any] = {"local_backends": {"cpu": backend}}
    base.update(over)
    cfg = api_config(data_dir, **base)
    cfg.local.probe_interval_s = 1.0
    cfg.local.poll_s = 0.2
    cfg.api.wait_poll_s = 0.1
    cfg.api.keepalive_s = 1.0
    return cfg


class LiveStack:
    def __init__(self, tmp: Path, cfg_over: dict[str, Any] | None = None) -> None:
        self.tmp = tmp
        self.llama = RichLlama()
        self.llama_port = free_port()
        self.port = free_port()
        self.cfg = live_config(tmp, self.llama_port, **(cfg_over or {}))
        self.url = f"http://127.0.0.1:{self.port}"
        self.loop: asyncio.AbstractEventLoop | None = None
        self.svc: Service | None = None
        self._servers: list[uvicorn.Server] = []
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._thread_main, name="llmhub-live", daemon=True)

    # --- thread -----------------------------------------------------------------------
    def _thread_main(self) -> None:
        try:
            asyncio.run(self._main())
        except BaseException as exc:  # noqa: BLE001 - surfaced by start()
            self._error = exc
            self._ready.set()

    async def _main(self) -> None:
        self.loop = asyncio.get_running_loop()
        clock = SystemClock()
        provider = MockProvider(MockStore(self.tmp / "mock_cloud.db"), clock, MockKnobs(boot_s=0.5))
        inference = MockInference(
            provider, clock, MockInferenceKnobs(load_s=0.2, pp_tps=1e6, tg_tps=1e6)
        )
        self.svc = build_service(
            self.cfg, ENV, clock, adapters={"mock": provider}, inference={"mock": inference}
        )
        app = create_app(self.svc.rt, run_loops=True)
        llama = uvicorn.Server(uvicorn.Config(self.llama, host="127.0.0.1", port=self.llama_port,
                                              log_config=None, lifespan="off"))  # fmt: skip
        api = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                            log_config=None, lifespan="on"))  # fmt: skip
        self._servers = [llama, api]
        tasks = [asyncio.create_task(s.serve()) for s in self._servers]
        while not all(s.started for s in self._servers):
            if any(t.done() for t in tasks):
                await asyncio.gather(*tasks)
                raise RuntimeError("live server failed to start")
            await asyncio.sleep(0.02)
        self._ready.set()
        try:
            await asyncio.gather(*tasks)
        finally:
            await self.svc.aclose()
            self.svc.rt.conn.close()

    # --- control (main thread) ------------------------------------------------------------
    def start(self) -> LiveStack:
        self._thread.start()
        if not self._ready.wait(30):
            raise RuntimeError("live stack did not start in 30 s")
        if self._error is not None:
            raise self._error
        return self

    def stop(self) -> None:
        for s in self._servers:
            s.should_exit = True
        self._thread.join(30)

    def run[T](self, coro: Coroutine[Any, Any, T], timeout: float = 30) -> T:
        assert self.loop is not None
        fut: Future[T] = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout)

    def call[T](self, fn: Callable[[], T], timeout: float = 30) -> T:
        async def wrapped() -> T:
            return fn()

        return self.run(wrapped(), timeout)

    def query(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        assert self.svc is not None
        conn = self.svc.rt.conn
        return self.call(lambda: [dict(r) for r in conn.execute(sql, params).fetchall()])

    def job(self, job_id: str) -> dict[str, Any]:
        (row,) = self.query("SELECT * FROM jobs WHERE id=?", job_id)
        return row

    def waiters(self) -> int:
        assert self.svc is not None
        waiter = self.svc.rt.waiter
        return self.call(lambda: waiter.count)

    def wait_for(self, cond: Callable[[], bool], timeout: float = 30) -> None:
        end = time.monotonic() + timeout
        while not cond():
            if time.monotonic() > end:
                raise AssertionError("condition not reached in time")
            time.sleep(0.05)

    def client(self, key: str = GAME_KEY, **kw: Any) -> httpx.Client:
        return httpx.Client(
            base_url=self.url, headers={"Authorization": f"Bearer {key}"}, timeout=60, **kw
        )

    def env(self) -> dict[str, str]:
        """Environment of the examples: base URL and keys only."""
        return {
            "PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", str(self.tmp)),
            "LLMHUB_URL": self.url, "LLMHUB_KEY": GAME_KEY, "LLMHUB_ADMIN_KEY": ADMIN_KEY,
            "OPENAI_BASE_URL": self.url + "/v1", "OPENAI_API_KEY": GAME_KEY,
        }  # fmt: skip


def run_serve_process(config: Path, env: dict[str, str]) -> subprocess.Popen[str]:
    """`python -m llmhub serve --config ...` as a child process (stopped by its own PID)."""
    return subprocess.Popen(
        [sys.executable, "-m", "llmhub", "serve", "--config", str(config)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )  # fmt: skip
