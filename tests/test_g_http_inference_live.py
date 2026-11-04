"""G5: HttpInferenceBackend against the REAL pod front (`llmhub_infer.front`) over localhost TLS.

uvicorn serves `build_app` on 127.0.0.1:0; llama-server is a fake ASGI upstream. Real sockets,
real TLS handshakes, real httpx transport (the default factory of the backend).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import ssl
import threading
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import uvicorn
from llmhub_infer.front import PodState, build_app
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from i_infer_fake import chat_body, sse_from_chat
from llmhub.cloud_manager.http_inference import HttpInferenceBackend, JobRunError
from llmhub.cloud_manager.inference import JobRunSpec, NetworkLost
from llmhub.cloud_manager.instance_env import InstanceSecrets, derive_api_key, derive_tls
from llmhub.persistence.db import connect, migrate
from tls_certs import make_self_signed
from w_fixtures import INSTANCE_SECRET, SECRETS, weights_cfg

OP = "0123456789ab"
FAST = {"connect_s": 2, "ping_s": 2, "health_s": 2, "test_s": 2, "read_s": 0.5,
        "job_cap_factor": 3.0}  # fmt: skip


class Llama:
    """Fake llama-server behind the front."""

    def __init__(self) -> None:
        self.mode = "ok"  # ok | drop | stall

    def app(self) -> Starlette:
        async def health(request: Request) -> Response:
            return JSONResponse({"status": "ok"})

        async def chat(request: Request) -> Response:
            raw = await request.body()
            wants_stream = json.loads(raw).get("stream") is True
            # fix round 1 F1: the backend always asks for stream:true -> SSE like llama-server
            payload = sse_from_chat(chat_body()) if wants_stream else json.dumps(chat_body()).encode()
            media = "text/event-stream" if wants_stream else "application/json"
            mode = self.mode

            async def gen() -> AsyncIterator[bytes]:
                if mode == "ok":
                    yield payload
                    return
                yield payload[:20]
                if mode == "stall":
                    await asyncio.sleep(30)
                raise RuntimeError("llama died mid-body")

            return StreamingResponse(gen(), media_type=media)

        return Starlette(
            routes=[Route("/health", health), Route("/v1/chat/completions", chat, methods=["POST"])]
        )


@contextlib.contextmanager
def _serve(app: Starlette, kw: dict[str, str]) -> Iterator[int]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", **kw))  # type: ignore[arg-type]
    t = threading.Thread(target=lambda: server.run([sock]), daemon=True)
    t.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.01)
    try:
        yield port
    finally:
        server.should_exit = True
        t.join(5)
        sock.close()


@contextlib.contextmanager
def serve_front(llama: Llama, key: str, tmp: Path, cert_pem: str | None,
                key_pem: str | None) -> Iterator[int]:  # fmt: skip
    """Real front over (optional) TLS, in front of a real-HTTP fake llama (true streaming)."""
    kw: dict[str, str] = {}
    if cert_pem is not None and key_pem is not None:
        (tmp / "cert.pem").write_text(cert_pem)
        (tmp / "key.pem").write_text(key_pem)
        kw = {"ssl_certfile": str(tmp / "cert.pem"), "ssl_keyfile": str(tmp / "key.pem")}
    with _serve(llama.app(), {}) as lport:
        state = PodState(source_names=("self_hosted", "hf"))
        state.set_phase("llama_load")
        app = build_app(state, key, upstream=f"http://127.0.0.1:{lport}")
        with _serve(app, kw) as port:
            yield port


class Live:
    def __init__(self, tmp: Path, transport: str, *, fingerprint: str | None = "derived",
                 proxy_ctx: ssl.SSLContext | None = None) -> None:  # fmt: skip
        self.created = time.time()
        self.cfg = weights_cfg(tmp, infer={"transport": transport, "client": FAST})
        self.conn = connect(tmp / "state.db", busy_timeout_ms=300)
        migrate(self.conn)
        self.cert, self.key_pem, fp = derive_tls(INSTANCE_SECRET, OP, self.created)
        self.fp = fp if fingerprint == "derived" else fingerprint
        self.pins: list[str] = []
        self.backend = HttpInferenceBackend(
            self.conn, SECRETS, self.cfg, on_pin_mismatch=self.pins.append,
            proxy_ssl_context=proxy_ctx,
        )  # fmt: skip
        self.api_key = derive_api_key(INSTANCE_SECRET, OP)

    def add(self, endpoint: str) -> None:
        self.conn.execute(
            "INSERT INTO instances(id, op_id, pool, provider, label, state, state_since,"
            " endpoint, tls_fingerprint, created_at, max_lifetime_at)"
            " VALUES ('i-1',?,'p1','mock','l',?,?,?,?,?,?)",
            (OP, "LOADING_MODEL", self.created, endpoint, self.fp, self.created,
             self.created + 10800),
        )  # fmt: skip


def job() -> JobRunSpec:
    return JobRunSpec("j-1", "m1", 100, 10, b'{"messages":[{"role":"user","content":"hi"}]}')


async def test_direct_derived_tls_full_cycle(tmp_path: Path) -> None:
    lv = Live(tmp_path, "direct")
    llama = Llama()
    with serve_front(llama, lv.api_key, tmp_path, lv.cert, lv.key_pem) as port:
        ep = f"https://127.0.0.1:{port}"
        lv.add(ep)
        assert await lv.backend.ping(ep)
        assert await lv.backend.health(ep)  # front asks fake llama /health -> ready
        st = await lv.backend.load_status(ep)
        assert st is not None and st.status == "ok"
        assert await lv.backend.test_request(ep)
        res = await lv.backend.run(ep, job())
        assert (res.tokens_in, res.tokens_out, res.finish_reason) == (11, 7, "length")
        # fix round 1 F2: a drop / stall AFTER the response headers counts an attempt
        llama.mode = "drop"
        with pytest.raises(JobRunError):
            await lv.backend.run(ep, job())
        llama.mode = "stall"
        with pytest.raises(JobRunError):
            await lv.backend.run(ep, job())
        await lv.backend.aclose()
    assert lv.pins == []


async def test_direct_foreign_certificate_pin_mismatch(tmp_path: Path) -> None:
    lv = Live(tmp_path, "direct")
    other = make_self_signed("127.0.0.1")
    with serve_front(Llama(), lv.api_key, tmp_path, other.cert_pem, other.key_pem) as port:
        ep = f"https://127.0.0.1:{port}"
        lv.add(ep)
        assert not await lv.backend.ping(ep)
        assert not await lv.backend.health(ep)
        with pytest.raises(NetworkLost):
            await lv.backend.run(ep, job())
        await lv.backend.aclose()
    assert lv.pins and set(lv.pins) == {ep}


async def test_direct_tampered_db_fingerprint(tmp_path: Path) -> None:
    lv = Live(tmp_path, "direct", fingerprint="f" * 64)
    with serve_front(Llama(), lv.api_key, tmp_path, lv.cert, lv.key_pem) as port:
        ep = f"https://127.0.0.1:{port}"
        lv.add(ep)
        assert not await lv.backend.ping(ep)  # handshake ok, sha256(DER) != DB
        await lv.backend.aclose()
    assert lv.pins == [ep]


async def test_proxy_mode_with_test_ca(tmp_path: Path) -> None:
    ca = make_self_signed("127.0.0.1")  # the "proxy" certificate, trusted via a test CA store
    ctx = ssl.create_default_context(cadata=ca.cert_pem)
    lv = Live(tmp_path, "proxy", fingerprint=None, proxy_ctx=ctx)
    with serve_front(Llama(), lv.api_key, tmp_path, ca.cert_pem, ca.key_pem) as port:
        ep = f"https://127.0.0.1:{port}"
        lv.add(ep)
        assert await lv.backend.ping(ep) and await lv.backend.health(ep)
        assert (await lv.backend.run(ep, job())).tokens_out == 7
        await lv.backend.aclose()
    # system CAs do not trust the test certificate; no pin callback in proxy mode
    lv2 = Live(tmp_path / "b", "proxy", fingerprint=None)
    with serve_front(Llama(), lv2.api_key, tmp_path, ca.cert_pem, ca.key_pem) as port:
        ep = f"https://127.0.0.1:{port}"
        lv2.add(ep)
        assert not await lv2.backend.ping(ep)
        await lv2.backend.aclose()
    assert lv2.pins == []


async def test_proxy_plain_http_rejected(tmp_path: Path) -> None:
    lv = Live(tmp_path, "proxy", fingerprint=None)
    with serve_front(Llama(), lv.api_key, tmp_path, None, None) as port:
        ep = f"http://127.0.0.1:{port}"
        lv.add(ep)
        assert not await lv.backend.ping(ep)
        with pytest.raises(NetworkLost):
            await lv.backend.run(ep, job())
        await lv.backend.aclose()


def test_secrets_type_hides_values() -> None:
    assert INSTANCE_SECRET.decode() not in repr(InstanceSecrets(INSTANCE_SECRET, b"w" * 32))
