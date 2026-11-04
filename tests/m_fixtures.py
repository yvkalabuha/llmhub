"""T-373 D1: MCP service on the in-process gateway (ApiEnv: FakeClock, StubWorker)."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from api_fixtures import GAME_KEY, ApiEnv
from llmhub.mcp_server.client import HubClient
from llmhub.mcp_server.kinds import load_kinds
from llmhub.mcp_server.paths import Roots
from llmhub.mcp_server.secret import Secret
from llmhub.mcp_server.service import McpService
from llmhub.mcp_server.settings import McpSettings


class Recorder(httpx.AsyncBaseTransport):
    """Delegating transport that records every request (headers and path)."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


def make_settings(tmp: Path, **over: Any) -> McpSettings:
    root = tmp / "proj"
    root.mkdir(exist_ok=True)
    base = McpSettings(
        url="http://t", key_env=None, key_file=tmp / "nokey", roots=(root,),
        scratch=tmp / "scratch", kinds_file=None, allow_cloud=False,
    )  # fmt: skip
    return dataclasses.replace(base, **over)


class Hub:
    """ApiEnv + Recorder + HubClient + McpService; the client sleeps by pumping the FakeClock."""

    def __init__(
        self,
        tmp: Path,
        *,
        key: str = GAME_KEY,
        key_fn: Callable[[], Secret] | None = None,
        **settings_over: Any,
    ) -> None:
        (tmp / "hub").mkdir(exist_ok=True)
        self.env = ApiEnv(tmp / "hub")
        self.settings = make_settings(tmp, **settings_over)
        self.root = self.settings.roots[0]
        self.transport = Recorder(httpx.ASGITransport(app=self.env.app,
                                                      raise_app_exceptions=False))  # fmt: skip
        self.client = HubClient(
            self.settings.url, key_fn or (lambda: Secret(key)),
            timeout_s=self.settings.http_timeout_s, transport=self.transport,
            sleep=self.env.pump, monotonic=self.env.clock.now,
        )  # fmt: skip
        self.roots = Roots.build(self.settings.roots, self.settings.scratch)
        self.service = McpService(self.settings, self.client, self.roots, load_kinds(None))

    @property
    def worker(self) -> Any:
        return self.env.worker

    async def aclose(self) -> None:
        await self.client.aclose()
        await self.env.aclose()
