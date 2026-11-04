"""FastAPI application factory and the process-wide Runtime."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from llmhub.api_gateway.auth import ClientRegistry
from llmhub.api_gateway.errors import ApiError, ErrorCode, install_handlers
from llmhub.api_gateway.protocols import CloudLike, WorkerLike
from llmhub.api_gateway.submission import Submitter
from llmhub.api_gateway.waiter import JobWaiter
from llmhub.clock import Clock
from llmhub.configuration.models import Config
from llmhub.controller import Controller
from llmhub.task_queue.bodies import BodyStore

log = logging.getLogger("llmhub.api")

LAG_TICK_S = 0.05


class LoopLag:
    """Event-loop stall monitor (real time): `llmhub_loop_lag_s` is the worst lag seen since
    the last read."""

    def __init__(self) -> None:
        self.max_lag_s = 0.0

    def take(self) -> float:
        value, self.max_lag_s = self.max_lag_s, 0.0
        return value

    async def run(self, stop: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            t0 = loop.time()
            await asyncio.sleep(LAG_TICK_S)
            self.max_lag_s = max(self.max_lag_s, loop.time() - t0 - LAG_TICK_S)


@dataclass
class Runtime:
    conn: Any  # sqlite3.Connection
    cfg: Config
    clock: Clock
    controller: Controller
    worker: WorkerLike
    bodies: BodyStore
    registry: ClientRegistry
    waiter: JobWaiter
    cloud: CloudLike
    started_at: float
    version: str
    config_path: Path | None = None
    lag: LoopLag = field(default_factory=LoopLag)
    http_counts: dict[tuple[str, int], int] = field(default_factory=dict)
    submitter: Submitter = field(init=False)

    def __post_init__(self) -> None:
        self.submitter = Submitter(self)


class _Edge:
    """Pure ASGI middleware: body size limit by Content-Length and request counters."""

    def __init__(self, app: ASGIApp, rt: Runtime) -> None:
        self.app = app
        self.rt = rt

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = 0

        async def counting_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        declared = dict(scope["headers"]).get(b"content-length")
        if (
            declared is not None
            and declared.isdigit()
            and int(declared) > self.rt.cfg.api.max_body_bytes
        ):
            resp = ApiError(ErrorCode.REQUEST_TOO_LARGE, "request body too large").response()
            await resp(scope, receive, counting_send)
        else:
            await self.app(scope, receive, counting_send)
        route = scope.get("route")
        path = getattr(route, "path", "unmatched")
        key = (path, status)
        self.rt.http_counts[key] = self.rt.http_counts.get(key, 0) + 1


def create_app(rt: Runtime, *, run_loops: bool = True) -> FastAPI:
    from llmhub.api_gateway import (
        routes_admin,
        routes_health,
        routes_leases,
        routes_openai,
        routes_tasks,
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        tasks: list[asyncio.Task[Any]] = []
        if run_loops:
            tasks = [
                asyncio.create_task(rt.controller.run_forever(stop)),
                asyncio.create_task(rt.worker.run_forever(stop)),
                asyncio.create_task(rt.waiter.run(stop)),
                asyncio.create_task(rt.lag.run(stop)),
            ]
        try:
            yield
        finally:
            stop.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="llmhub", version=rt.version, lifespan=lifespan)
    app.state.rt = rt
    install_handlers(app)
    for module in (routes_openai, routes_tasks, routes_leases, routes_admin, routes_health):
        app.include_router(module.router)
    app.add_middleware(_Edge, rt=rt)
    return app
