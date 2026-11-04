"""T-370 stream C1: pod front (health semantics, key check, streaming proxy)."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field

import httpx
import pytest
import uvicorn
from llmhub_infer import front
from llmhub_infer.front import PodState, build_app
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

KEY = "k-secret-123"
AUTH = {"Authorization": f"Bearer {KEY}"}


@dataclass
class P:
    phase: str = "download"
    file: str | None = "model"
    source: str | None = "self_hosted"
    done: int = 0
    total: int = 0
    bps: float = 0.0
    failovers: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None


@contextlib.contextmanager
def serve_thread(app: Starlette) -> Iterator[int]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    t = threading.Thread(target=lambda: server.run([sock]), daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.01)
    try:
        yield port
    finally:
        server.should_exit = True
        t.join(5)
        sock.close()


class Upstream:
    def __init__(self) -> None:
        self.health = 200
        self.seen: list[tuple[str, dict[str, str], bytes]] = []

    def app(self) -> Starlette:
        async def health(request: Request) -> Response:
            return JSONResponse({"status": "x"}, status_code=self.health)

        async def chat(request: Request) -> Response:
            self.seen.append((request.url.path, dict(request.headers), await request.body()))

            async def gen() -> AsyncIterator[bytes]:
                yield b"data: 1\n\n"
                await asyncio.sleep(0.05)
                yield b"data: 2\n\n"

            return StreamingResponse(gen(), media_type="text/event-stream")

        return Starlette(
            routes=[
                Route("/health", health),
                Route("/v1/chat/completions", chat, methods=["POST"]),
                Route("/props", chat),
            ]
        )


def make(state: PodState, up: Upstream) -> httpx.AsyncClient:
    app = build_app(state, KEY, upstream_transport=httpx.ASGITransport(app=up.app()))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://pod")


async def test_ping_open() -> None:
    async with make(PodState(), Upstream()) as c:
        assert (await c.get("/ping")).status_code == 204


async def test_health_requires_key() -> None:
    async with make(PodState(), Upstream()) as c:
        assert (await c.get("/health")).status_code == 401
        r = await c.get("/health", headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401


async def test_health_loading_has_progress_eta_sources() -> None:
    st = PodState(source_names=("self_hosted", "hf"))
    st.set_progress(P(done=1_000_000, total=11_000_000, bps=2_000_000.0))
    async with make(st, Upstream()) as c:
        r = await c.get("/health", headers=AUTH)
    assert r.status_code == 503
    j = r.json()
    assert j["status"] == "loading" and j["done"] == 1_000_000 and j["total"] == 11_000_000
    assert j["mb_s"] == 2.0 and j["eta_s"] == 5.0 and j["sources_left"] == 1
    assert j["source"] == "self_hosted" and j["file"] == "model" and j["failovers"] == []


async def test_health_last_source_has_none_left() -> None:
    st = PodState(source_names=("self_hosted", "hf"))
    st.set_progress(P(source="hf", total=10, done=1))
    async with make(st, Upstream()) as c:
        j = (await c.get("/health", headers=AUTH)).json()
    assert j["sources_left"] == 0 and j["eta_s"] is None


async def test_health_failed_500() -> None:
    st = PodState()
    st.fail("deadline")
    async with make(st, Upstream()) as c:
        r = await c.get("/health", headers=AUTH)
    assert r.status_code == 500 and r.json() == {"status": "failed", "reason": "deadline"}


async def test_health_ok_only_if_llama_healthy() -> None:
    st = PodState()
    st.set_phase("llama_load")
    up = Upstream()
    up.health = 503
    async with make(st, up) as c:
        r = await c.get("/health", headers=AUTH)
        assert r.status_code == 503 and r.json()["phase"] == "llama_load"
        up.health = 200
        r = await c.get("/health", headers=AUTH)
        assert r.status_code == 200 and r.json() == {"status": "ok"}


async def test_proxy_503_before_ready_then_streams_without_auth() -> None:
    st = PodState()
    up = Upstream()
    async with make(st, up) as c:
        r = await c.post("/v1/chat/completions", headers=AUTH, content=b"{}")
        assert r.status_code == 503
        st.set_phase("ready")
    with serve_thread(up.app()) as port:
        sent = await _call_chat(st, port)
    chunks = [m["body"] for m in sent if m["type"] == "http.response.body" and m["body"]]
    assert len(chunks) >= 2 and b"".join(chunks) == b"data: 1\n\ndata: 2\n\n"
    path, headers, body = up.seen[0]
    assert path == "/v1/chat/completions" and body == b'{"a":1}'
    assert "authorization" not in headers and headers.get("x-keep") == "1"


async def _call_chat(st: PodState, port: int) -> list[dict]:  # type: ignore[type-arg]
    app = build_app(st, KEY, upstream=f"http://127.0.0.1:{port}")
    sent: list[dict] = []  # type: ignore[type-arg]

    delivered = False

    async def receive() -> dict:  # type: ignore[type-arg]
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": b'{"a":1}', "more_body": False}
        await asyncio.Event().wait()  # client stays connected
        raise AssertionError

    async def send(msg: dict) -> None:  # type: ignore[type-arg]
        sent.append(msg)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "raw_path": b"",
        "query_string": b"",
        "scheme": "http",
        "server": ("pod", 80),
        "http_version": "1.1",
        "headers": [(b"authorization", f"Bearer {KEY}".encode()), (b"x-keep", b"1")],
    }
    await app(scope, receive, send)  # type: ignore[arg-type]
    return sent


async def test_proxy_requires_key_and_limits_body() -> None:
    st = PodState()
    st.set_phase("ready")
    async with make(st, Upstream()) as c:
        assert (await c.post("/v1/chat/completions", content=b"{}")).status_code == 401
        big = b"x" * (front.MAX_BODY + 1)
        r = await c.post("/v1/chat/completions", headers=AUTH, content=big)
        assert r.status_code == 413


async def test_unknown_path_404() -> None:
    async with make(PodState(), Upstream()) as c:
        assert (await c.get("/admin", headers=AUTH)).status_code == 404


async def test_key_compare_is_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(front.hmac, "compare_digest", spy)
    async with make(PodState(), Upstream()) as c:
        await c.get("/health", headers=AUTH)
    assert calls and calls[0][1] == KEY.encode()
