"""G4: HttpInferenceBackend against a fake llmhub-infer (in-process ASGI, no network)."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import ssl
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from i_infer_fake import FakeInfer, chat_body
from llmhub.clock import FakeClock
from llmhub.cloud_manager import http_inference as hi
from llmhub.cloud_manager.http_inference import HttpInferenceBackend, Timings, pin_incident_recorder
from llmhub.cloud_manager.inference import InstanceLost, JobRunSpec, NetworkLost
from llmhub.cloud_manager.instance_env import derive_api_key, derive_tls
from llmhub.cloud_manager.load_status import LoadStatus, LoadStatusSource
from llmhub.configuration.models import Config
from llmhub.persistence.db import connect, migrate
from w_fixtures import INSTANCE_SECRET, SECRETS, weights_cfg

OP = "0123456789ab"
EP = "https://pod-1.proxy.example"
FAST = {"connect_s": 1, "ping_s": 1, "health_s": 1, "test_s": 0.3, "read_s": 0.3,
        "job_cap_factor": 3.0}  # fmt: skip


def make_cfg(tmp: Path, transport: str = "proxy", **client: float) -> Config:
    return weights_cfg(tmp, infer={"transport": transport, "client": FAST | client})


def make_conn(tmp: Path) -> sqlite3.Connection:
    conn = connect(tmp / "state.db", busy_timeout_ms=300)
    migrate(conn)
    return conn


def add_inst(
    conn: sqlite3.Connection,
    iid: str = "i-1",
    op: str = OP,
    endpoint: str = EP,
    state: str = "READY",
    pool: str = "p1",
    fingerprint: str | None = None,
    created_at: float = 1_762_000_000.0,
) -> None:
    conn.execute(
        "INSERT INTO instances(id, op_id, pool, provider, label, state, state_since, endpoint,"
        " tls_fingerprint, created_at, max_lifetime_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (iid, op, pool, "mock", f"llmhub-{op}-00000000", state, created_at, endpoint,
         fingerprint, created_at, created_at + 10800),
    )  # fmt: skip


class _SSLObj:
    def __init__(self, der: bytes) -> None:
        self.der = der

    def getpeercert(self, binary_form: bool = False) -> bytes:
        return self.der


class _Stream:
    def __init__(self, der: bytes | None) -> None:
        self.der = der

    def get_extra_info(self, name: str) -> Any:
        return _SSLObj(self.der) if name == "ssl_object" and self.der is not None else None


class PeerCertTransport(httpx.AsyncBaseTransport):
    """ASGI transport that pretends the TLS peer presented `der` (direct-mode pin check)."""

    def __init__(self, inner: httpx.AsyncBaseTransport, der: bytes | None) -> None:
        self.inner = inner
        self.der = der

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        resp = await self.inner.handle_async_request(request)
        resp.extensions = {**resp.extensions, "network_stream": _Stream(self.der)}
        return resp


class Env:
    def __init__(self, tmp: Path, transport: str = "proxy", **client: float) -> None:
        self.cfg = make_cfg(tmp, transport, **client)
        self.conn = make_conn(tmp)
        self.fake = FakeInfer(key=derive_api_key(INSTANCE_SECRET, OP))
        self.timings: list[tuple[str, Timings]] = []
        self.pins: list[str] = []
        self.ctxs: list[ssl.SSLContext] = []
        self.peer_der: bytes | None = None
        self.backend = HttpInferenceBackend(
            self.conn, SECRETS, self.cfg, transport_factory=self._factory,
            on_timings=lambda j, t: self.timings.append((j, t)),
            on_pin_mismatch=self.pins.append,
        )  # fmt: skip

    def _factory(self, ctx: ssl.SSLContext) -> httpx.AsyncBaseTransport:
        self.ctxs.append(ctx)
        inner = httpx.ASGITransport(app=self.fake.app())
        return PeerCertTransport(inner, self.peer_der) if self.peer_der is not None else inner


def job(body: bytes = b'{"messages":[{"role":"user","content":"hi"}]}') -> JobRunSpec:
    return JobRunSpec(job_id="j-1", model_alias="m1", tokens_in=100, max_tokens=10, body=body)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    e = Env(tmp_path)
    add_inst(e.conn)
    return e


def test_implements_load_status_source(env: Env) -> None:
    assert isinstance(env.backend, LoadStatusSource)


# --- ping / health / test --------------------------------------------------------------------
async def test_ping(env: Env) -> None:
    assert await env.backend.ping(EP)
    env.fake.proxy_no_pod = True
    assert not await env.backend.ping(EP)


async def test_ping_exception_is_false(tmp_path: Path) -> None:
    e = Env(tmp_path)
    add_inst(e.conn)

    class Boom(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

    e.backend._factory = lambda ctx: Boom()
    assert not await e.backend.ping(EP)
    assert not await e.backend.health(EP)
    assert await e.backend.load_status(EP) is None
    with pytest.raises(NetworkLost):
        await e.backend.run(EP, job())


@pytest.mark.parametrize(("phase", "ok"), [("ok", True), ("loading", False), ("failed", False),
                                           ("llama_load", False)])  # fmt: skip
async def test_health(env: Env, phase: str, ok: bool) -> None:
    env.fake.phase = phase
    assert await env.backend.health(EP) is ok


async def test_health_wrong_key_false(env: Env) -> None:
    env.fake.key = derive_api_key(INSTANCE_SECRET, "ba9876543210")  # another op's pod
    assert not await env.backend.health(EP)


async def test_test_request(env: Env) -> None:
    assert await env.backend.test_request(EP)
    path, headers, raw = env.fake.seen[-1]
    sent = json.loads(raw)
    assert path == "/v1/chat/completions" and sent["max_tokens"] == 8 and sent["temperature"] == 0
    assert headers["authorization"] == "Bearer " + derive_api_key(INSTANCE_SECRET, OP)
    env.fake.chat = chat_body(choices=0)
    assert not await env.backend.test_request(EP)
    env.fake.chat = chat_body()
    env.fake.phase = "failed"
    assert not await env.backend.test_request(EP)


async def test_test_request_slow_is_false(env: Env) -> None:
    env.fake.delay_s = 2.0  # > test_s (0.3) + connect (1)
    assert not await env.backend.test_request(EP)


# --- run -------------------------------------------------------------------------------------
async def test_run_happy_path(env: Env) -> None:
    body = b'{"messages":[{"role":"user","content":"\\u043f\\u0440\\u0438\\u0432\\u0435\\u0442"}]}'
    res = await env.backend.run(EP, job(body))
    path, headers, raw = env.fake.seen[-1]
    # fix round 1 F1: the pod always gets stream:true; the SSE answer is assembled
    sent = json.loads(raw)
    assert sent == {**json.loads(body), "stream": True, "stream_options": {"include_usage": True}}
    assert path == "/v1/chat/completions"
    assert headers["authorization"] == "Bearer " + derive_api_key(INSTANCE_SECRET, OP)
    assert (res.tokens_in, res.tokens_out, res.finish_reason) == (11, 7, "length")
    assert json.loads(res.body) == env.fake.chat
    assert env.timings and env.timings[0][0] == "j-1" and env.timings[0][1].draft_n == 60


async def test_run_tokens_from_timings_without_usage(env: Env) -> None:
    env.fake.chat = chat_body(usage=False)
    res = await env.backend.run(EP, job())
    assert (res.tokens_in, res.tokens_out) == (1200, 78)
    env.fake.chat = chat_body(usage=False, timings=False)
    res = await env.backend.run(EP, job())
    assert (res.tokens_in, res.tokens_out) == (100, 0)


async def test_run_llama_400_is_error_result(env: Env) -> None:
    env.fake.chat_status = 400
    env.fake.chat_raw = b'{"error":{"code":400,"message":"context too long"}}'
    res = await env.backend.run(EP, job())
    assert res.finish_reason == "error" and res.body == env.fake.chat_raw


async def test_run_header_wait_is_bounded_by_cap_not_read_s(env: Env) -> None:
    """F1: the in-process fake buffers the whole answer, so a 1.5 s generation is a 1.5 s
    header wait: > read_s + connect_s, < job cap -> a normal result (was NetworkLost)."""
    env.fake.delay_s = 1.5  # read_s 0.3 (+ connect 1)
    res = await env.backend.run(EP, job())
    assert res.finish_reason == "length"


async def test_run_job_cap_counts_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F2: the job cap is not a network loss: JobRunError (the core counts the attempt)."""
    monkeypatch.setattr(hi, "JOB_CAP_BASE_S", 0.0)
    e = Env(tmp_path, read_s=10, job_cap_factor=0.001)
    add_inst(e.conn)
    e.fake.delay_s = 1.0  # within read_s, beyond the cap (~0.1 s * 0.001)
    with pytest.raises(hi.JobRunError):
        await e.backend.run(EP, job())


async def test_run_proxy_without_pod_network_lost(env: Env) -> None:
    env.fake.proxy_no_pod = True
    with pytest.raises(NetworkLost):
        await env.backend.run(EP, job())


async def test_run_failed_pod_instance_lost(env: Env) -> None:
    env.fake.phase = "failed"
    with pytest.raises(InstanceLost):
        await env.backend.run(EP, job())


async def test_run_restarted_pod_instance_lost(env: Env) -> None:
    env.fake.phase = "loading"
    with pytest.raises(InstanceLost):
        await env.backend.run(EP, job())


async def test_run_401_instance_lost(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    env.fake.key = derive_api_key(INSTANCE_SECRET, "ba9876543210")
    with caplog.at_level(logging.ERROR, "llmhub.http_inference"), pytest.raises(InstanceLost):
        await env.backend.run(EP, job())
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


async def test_run_other_5xx_json_from_llama(env: Env) -> None:
    env.fake.chat_status = 500
    env.fake.chat_raw = b'{"error":{"code":500,"message":"grammar"}}'
    res = await env.backend.run(EP, job())
    assert res.finish_reason == "error"
    env.fake.chat_status = 503
    with pytest.raises(NetworkLost):  # not our front's JSON
        await env.backend.run(EP, job())


async def test_run_cancel_propagates(env: Env) -> None:
    env.fake.delay_s = 5.0
    task = asyncio.ensure_future(env.backend.run(EP, job()))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- load_status -----------------------------------------------------------------------------
async def test_load_status_phases(env: Env) -> None:
    env.fake.phase = "loading"
    st = await env.backend.load_status(EP)
    assert st == LoadStatus(status="loading", phase="downloading", source="self_hosted", done=100,
                            total=1000, mb_s=50.0, eta_s=18.0, sources_left=1)  # fmt: skip
    env.fake.phase = "llama_load"
    st = await env.backend.load_status(EP)
    assert st is not None and st.status == "loading" and st.phase == "llama_load"
    env.fake.phase = "failed"
    assert await env.backend.load_status(EP) == LoadStatus(status="failed", reason="weights")
    env.fake.phase = "ok"
    assert await env.backend.load_status(EP) == LoadStatus(status="ok")


async def test_load_status_garbage_is_none(env: Env) -> None:
    env.fake.health_html = True
    assert await env.backend.load_status(EP) is None
    env.fake.health_html = False
    env.fake.key = "other"
    assert await env.backend.load_status(EP) is None


# --- lookup by instance row ------------------------------------------------------------------
async def _all_false(b: HttpInferenceBackend, ep: str) -> None:
    assert not await b.ping(ep)
    assert not await b.health(ep)
    assert not await b.test_request(ep)
    assert await b.load_status(ep) is None
    with pytest.raises(NetworkLost):
        await b.run(ep, job())


async def test_unknown_endpoint(env: Env) -> None:
    await _all_false(env.backend, "https://other.example")


async def test_dead_row_is_not_used(tmp_path: Path) -> None:
    e = Env(tmp_path)
    add_inst(e.conn, state="STOPPED")
    await _all_false(e.backend, EP)


async def test_two_live_rows_same_endpoint(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    add_inst(env.conn, iid="i-2", op="ba9876543210", pool="p2")
    with caplog.at_level(logging.ERROR, "llmhub.http_inference"):
        await _all_false(env.backend, EP)
    assert any("2 live instances" in r.getMessage() for r in caplog.records)


async def test_http_endpoint_rejected(tmp_path: Path) -> None:
    e = Env(tmp_path)
    add_inst(e.conn, endpoint="http://pod-1.proxy.example")
    await _all_false(e.backend, "http://pod-1.proxy.example")


async def test_no_key_in_logs(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    await env.backend.health(EP)
    await env.backend.run(EP, job())
    env.fake.key = "x"
    with pytest.raises(InstanceLost):
        await env.backend.run(EP, job())
    assert derive_api_key(INSTANCE_SECRET, OP) not in caplog.text


async def test_client_cache_per_row_and_state(env: Env) -> None:
    await env.backend.ping(EP)
    await env.backend.ping(EP)
    assert len(env.ctxs) == 1
    env.conn.execute("UPDATE instances SET state='BUSY' WHERE id='i-1'")
    await env.backend.ping(EP)
    assert len(env.ctxs) == 2
    await env.backend.aclose()


# --- direct mode pin (ASGI with a pretend peer certificate) ----------------------------------
def _direct(tmp: Path, fp: Callable[[str], str | None] = lambda real: real) -> Env:
    e = Env(tmp, "direct")
    cert, _, real = derive_tls(INSTANCE_SECRET, OP, 1_762_000_000.0)
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding

    e.peer_der = x509.load_pem_x509_certificate(cert.encode()).public_bytes(Encoding.DER)
    add_inst(e.conn, fingerprint=fp(real))
    return e


async def test_direct_pin_ok(tmp_path: Path) -> None:
    e = _direct(tmp_path)
    assert await e.backend.ping(EP) and await e.backend.health(EP)
    assert (await e.backend.run(EP, job())).tokens_out == 7
    ctx = e.ctxs[0]
    assert ctx.verify_mode == ssl.CERT_REQUIRED and not ctx.check_hostname
    assert ctx.cert_store_stats()["x509"] == 1  # the derived certificate is the only anchor
    assert e.pins == []


async def test_direct_fingerprint_mismatch(tmp_path: Path) -> None:
    e = _direct(tmp_path, lambda real: "0" * 64)
    assert not await e.backend.ping(EP)
    assert not await e.backend.health(EP)
    with pytest.raises(NetworkLost):
        await e.backend.run(EP, job())
    assert e.pins and set(e.pins) == {EP}


async def test_direct_missing_fingerprint_fails_closed(tmp_path: Path) -> None:
    e = _direct(tmp_path, lambda real: None)
    assert not await e.backend.ping(EP)
    assert e.pins == [EP]


async def test_pin_incident_recorder(env: Env) -> None:
    rec = pin_incident_recorder(env.conn, FakeClock())
    rec(EP)
    rec(EP)  # deduplicated within the hour
    rows = env.conn.execute("SELECT * FROM incidents WHERE kind='tls_pin_mismatch'").fetchall()
    assert len(rows) == 1 and rows[0]["severity"] == "high" and rows[0]["ref"] == "i-1"
