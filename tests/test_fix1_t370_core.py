"""T-370 fix round 1: F1/F2/F3/F4/F6/F8/F9 (backend side) - streaming, NetworkLost bound,
early-E7 by average speed, progress-key cleanup, config errors without blacklist, client prune.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from harness import Harness, make_job
from i_infer_fake import chat_body, sse_from_chat
from llmhub.cloud_manager import http_inference as hi
from llmhub.cloud_manager.http_inference import HttpInferenceBackend, assemble_sse, force_stream
from llmhub.cloud_manager.inference import JobRunResult, JobRunSpec, NetworkLost
from llmhub.cloud_manager.load_status import LoadStatus
from llmhub.cloud_manager.provisioner import SLOW_CONFIRM_S
from llmhub.lifecycle_manager import machine
from llmhub.provider_adapters.mock_inference import MockInferenceKnobs
from test_g_http_inference import EP, Env, add_inst, job, make_cfg, make_conn
from test_w_provisioner_loading import make_h, reasons
from w_fixtures import SECRETS


# --- scripted streaming transport (real chunk timing; the ASGI fake buffers everything) -----
class _Script(httpx.AsyncByteStream):
    def __init__(self, parts: list[tuple[float, bytes]], fail: BaseException | None) -> None:
        self.parts = parts
        self.fail = fail

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for delay, data in self.parts:
            if delay:
                await asyncio.sleep(delay)
            yield data
        if self.fail is not None:
            raise self.fail


class ScriptedTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        parts: list[tuple[float, bytes]] | None = None,
        *,
        status: int = 200,
        ctype: str = "text/event-stream",
        fail: BaseException | None = None,
        before: BaseException | None = None,
    ) -> None:
        self.parts = parts or []
        self.status = status
        self.ctype = ctype
        self.fail = fail
        self.before = before
        self.bodies: list[bytes] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(await request.aread())
        if self.before is not None:
            raise self.before
        return httpx.Response(
            self.status, headers={"content-type": self.ctype},
            stream=_Script(self.parts, self.fail),
        )  # fmt: skip


def _backend(tmp: Path, tr: httpx.AsyncBaseTransport, **client: float) -> HttpInferenceBackend:
    cfg = make_cfg(tmp, "proxy", **client)
    conn = make_conn(tmp)
    add_inst(conn)

    def factory(ctx: ssl.SSLContext) -> httpx.AsyncBaseTransport:
        return tr

    return HttpInferenceBackend(conn, SECRETS, cfg, transport_factory=factory)


def _sse_parts(text: str, every_s: float) -> list[tuple[float, bytes]]:
    raw = sse_from_chat(chat_body() | {"choices": [{"index": 0, "message": {
        "role": "assistant", "content": text}, "finish_reason": "stop"}]})  # fmt: skip
    events = [e + b"\n\n" for e in raw.split(b"\n\n") if e]
    return [(every_s, e) for e in events]


# --- F1 -----------------------------------------------------------------------------------
def test_force_stream_sets_stream_and_usage() -> None:
    out = json.loads(force_stream(b'{"messages":[],"stream":false,"stream_options":{"x":1}}'))
    assert out["stream"] is True and out["stream_options"] == {"x": 1, "include_usage": True}
    assert force_stream(b"not json") == b"not json"
    assert force_stream(b"[1]") == b"[1]"


def test_assemble_sse_roundtrip_and_tool_calls() -> None:
    chat = chat_body()
    data, complete = assemble_sse(sse_from_chat(chat))
    assert complete and data == chat
    tool = {"id": "c1", "type": "function", "function": {"name": "f", "arguments": '{"a":1}'}}
    chat2 = chat_body() | {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "", "tool_calls": [tool]},
                "finish_reason": "tool_calls",
            }
        ]
    }
    data2, _ = assemble_sse(sse_from_chat(chat2))
    assert data2 is not None and data2["choices"][0]["message"]["tool_calls"] == [tool]


def test_assemble_sse_truncated_and_error() -> None:
    raw = sse_from_chat(chat_body(), done=False)
    cut = raw[: raw.index(b'"finish_reason": "length"')]  # drop before the finish chunk
    assert assemble_sse(cut)[1] is False
    assert assemble_sse(b"")[0] is None
    err, complete = assemble_sse(b'error: {"code":500,"message":"boom"}\n\n')
    assert complete and err == {"error": {"code": 500, "message": "boom"}}


async def test_long_streamed_job_beyond_read_s_is_ok(tmp_path: Path) -> None:
    """2 s of tokens (> read_s 0.3 + connect 1), bytes every 0.05 s -> a normal result."""
    tr = ScriptedTransport(_sse_parts("x" * 80, 0.05))
    b = _backend(tmp_path, tr)
    res = await b.run(EP, job())
    assert res.finish_reason == "stop" and json.loads(res.body)["choices"][0]["message"][
        "content"] == "x" * 80  # fmt: skip
    assert json.loads(tr.bodies[0])["stream"] is True
    await b.aclose()


async def test_pause_after_headers_counts_attempt(tmp_path: Path) -> None:
    parts = _sse_parts("hello", 0.0)
    parts[2] = (2.0, parts[2][1])  # a stall > read_s after some bytes
    b = _backend(tmp_path, ScriptedTransport(parts))
    with pytest.raises(hi.JobRunError):
        await b.run(EP, job())
    await b.aclose()


async def test_drop_after_headers_counts_attempt(tmp_path: Path) -> None:
    parts = _sse_parts("hello", 0.0)[:2]
    b = _backend(tmp_path, ScriptedTransport(parts, fail=httpx.ReadError("reset")))
    with pytest.raises(hi.JobRunError):
        await b.run(EP, job())
    await b.aclose()


async def test_truncated_stream_counts_attempt(tmp_path: Path) -> None:
    b = _backend(tmp_path, ScriptedTransport(_sse_parts("hello", 0.0)[:2]))
    with pytest.raises(hi.JobRunError):
        await b.run(EP, job())
    await b.aclose()


async def test_connect_error_before_headers_is_network_lost(tmp_path: Path) -> None:
    b = _backend(tmp_path, ScriptedTransport(before=httpx.ConnectError("refused")))
    with pytest.raises(NetworkLost):
        await b.run(EP, job())
    await b.aclose()


async def test_sse_error_event_is_error_result(tmp_path: Path) -> None:
    tr = ScriptedTransport([(0.0, b'data: {"error":{"code":500,"message":"x"}}\n\n')])
    b = _backend(tmp_path, tr)
    res = await b.run(EP, job())
    assert res.finish_reason == "error"
    await b.aclose()


@pytest.mark.parametrize("status", [520, 524])
async def test_proxy_52x_is_network_lost(tmp_path: Path, status: int) -> None:
    tr = ScriptedTransport([(0.0, b"<html>timeout</html>")], status=status, ctype="text/html")
    b = _backend(tmp_path, tr)
    with pytest.raises(NetworkLost):
        await b.run(EP, job())
    await b.aclose()


# --- F9 (backend side) ----------------------------------------------------------------------
async def test_front_restarting_is_network_lost(tmp_path: Path) -> None:
    e = Env(tmp_path)
    add_inst(e.conn)
    e.fake.restarting = True
    with pytest.raises(NetworkLost):
        await e.backend.run(EP, job())


# --- F8 -----------------------------------------------------------------------------------
async def test_clients_of_finished_instances_are_closed(tmp_path: Path) -> None:
    e = Env(tmp_path)
    add_inst(e.conn)
    assert await e.backend.ping(EP)
    (client,) = [c for _, c in e.backend._clients.values()]
    e.conn.execute("UPDATE instances SET state='STOPPED' WHERE id='i-1'")
    assert not await e.backend.ping(EP)
    assert e.backend._clients == {} and client.is_closed


# --- F2 (core bound) ------------------------------------------------------------------------
async def test_network_lost_bound_runs_and_instance_lost(tmp_path: Path) -> None:
    """Always NetworkLost with a live front: <= 2 x job_max_attempts runs, then local
    fallback; 3 in a row on one instance -> the instance is lost (no 45-run loop)."""
    h = Harness(tmp_path)
    runs: list[str] = []

    async def always_lost(endpoint: str, spec: JobRunSpec) -> JobRunResult:
        runs.append(endpoint)
        await h.clock.sleep_until(h.clock.now() + 130)
        raise NetworkLost("timeout")

    h.inference.run = always_lost  # type: ignore[method-assign]
    h.submit(make_job(0))
    await h.run_for(2 * 3600)
    job_row = h.jobs()[0]
    assert len(runs) <= 2 * h.cfg.retry.job_max_attempts
    assert job_row["status"] != "running"
    assert any(r == "network_lost_repeated" for _, _, r in h.transitions())


async def test_single_network_lost_is_free(tmp_path: Path) -> None:
    """One blip: the attempt is refunded and the job completes on the next run."""
    h = Harness(tmp_path)
    inner = h.inference.run
    calls = {"n": 0}

    async def blip(endpoint: str, spec: JobRunSpec) -> JobRunResult:
        calls["n"] += 1
        if calls["n"] == 1:
            raise NetworkLost("blip")
        return await inner(endpoint, spec)

    h.inference.run = blip  # type: ignore[method-assign]
    h.submit(make_job(0))
    await h.run_for(1800)
    row = h.jobs()[0]
    assert row["status"] == "completed" and row["attempts"] == 1, dict(row)


# --- F3 -------------------------------------------------------------------------------------
def _with_bytes(h: Harness, done_fn: object, total: int, pod_eta: float) -> None:
    inner = h.inference.load_status

    async def load_status(endpoint: str) -> LoadStatus | None:
        st = await inner(endpoint)
        if st is None or st.status != "loading":
            return st
        inst = h.instances()[-1]
        elapsed = h.clock.now() - float(inst["state_since"])
        return replace(st, done=int(done_fn(elapsed)), total=total, eta_s=pod_eta)  # type: ignore[operator]

    h.inference.load_status = load_status  # type: ignore[method-assign]


async def test_dip_with_good_average_is_not_killed(tmp_path: Path) -> None:
    """The pod's 10 s-window ETA is huge (a dip), the average since LOADING_MODEL fits."""
    knobs = MockInferenceKnobs(hang_load=True, load_sources_left=0)
    h = make_h(tmp_path, knobs=knobs)
    total = 10_000_000_000
    _with_bytes(h, lambda el: min(total - 1, el * 20e6), total, pod_eta=99999.0)  # 20 MB/s
    h.submit(make_job(0))
    await h.run_until(h.clock.now() + 400)
    assert "weights_too_slow" not in reasons(h)


async def test_slow_average_is_killed_after_confirm(tmp_path: Path) -> None:
    knobs = MockInferenceKnobs(hang_load=True, load_sources_left=0)
    h = make_h(tmp_path, knobs=knobs)
    total = 10_000_000_000
    _with_bytes(h, lambda el: el * 1e6, total, pod_eta=1.0)  # 1 MB/s: ~2.8 h, pod ETA lies
    h.submit(make_job(0))
    await h.run_until(h.clock.now() + 400)
    assert "weights_too_slow" in reasons(h)
    tr = h.conn.execute(
        "SELECT ts FROM transitions WHERE to_state IN ('LOADING_MODEL','ERROR') ORDER BY id"
    ).fetchall()
    assert float(tr[1][0]) - float(tr[0][0]) >= 60 + SLOW_CONFIRM_S


# --- F4 -------------------------------------------------------------------------------------
async def test_mark_lost_from_loading_clears_progress_keys(tmp_path: Path) -> None:
    knobs = MockInferenceKnobs(hang_load=True, load_eta_s=100, load_sources_left=1)
    h = make_h(tmp_path, knobs=knobs)
    h.submit(make_job(0))
    await h.run_until(h.clock.now() + 120)
    inst = h.instances()[0]
    assert inst["state"] == "LOADING_MODEL"
    key = f"load_progress:{inst['id']}"
    assert h.conn.execute("SELECT 1 FROM controller_state WHERE key=?", (key,)).fetchone()
    h.conn.execute(
        "INSERT OR REPLACE INTO controller_state(key, value, updated_at) VALUES (?,?,?)",
        (f"load_slow_since:{inst['id']}", "1", 0.0),
    )
    assert machine.mark_lost(h.conn, inst["id"], h.clock.now(), "resource_gone", cfg=h.cfg)
    assert (
        h.conn.execute(
            "SELECT COUNT(*) FROM controller_state WHERE key LIKE ?", (f"load_%{inst['id']}",)
        ).fetchone()[0]
        == 0
    )


# --- F6 -------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("reason", "causes", "blacklisted"),
    [("exhausted", ("sha_mismatch",), False), ("exhausted", ("http_4xx", "size_mismatch"), False),
     ("sha_mismatch", (), False), ("size_mismatch", (), False),
     # fix round 2 (R2-1): `exhausted` without causes or with any non-config cause -> host
     ("exhausted", (), True), ("exhausted", ("network", "sha_mismatch"), True),
     ("disk", (), True), ("deadline", (), True)],
)  # fmt: skip
async def test_config_failures_do_not_blacklist_host(
    tmp_path: Path, reason: str, causes: tuple[str, ...], blacklisted: bool
) -> None:
    knobs = MockInferenceKnobs(load_fail=reason, load_causes=causes)
    h = make_h(tmp_path, knobs=knobs)
    h.submit(make_job(0))
    await h.run_until(h.clock.now() + 60)
    assert f"weights_failed:{reason}" in reasons(h)
    n = h.conn.execute("SELECT COUNT(*) FROM host_blacklist").fetchone()[0]
    assert (n > 0) is blacklisted
    assert bool(h.incidents("weights_config")) is (not blacklisted)


# --- SEC-9 ------------------------------------------------------------------------------------
@pytest.mark.parametrize("listen", ["127.0.0.1:8096", "[::1]:8096"])
def test_auth_listen_loopback_ok(listen: str) -> None:
    from llmhub.configuration.weights_models import WeightsConfig

    assert WeightsConfig(auth_listen=listen).auth_listen == listen


@pytest.mark.parametrize(
    "listen", ["0.0.0.0:8096", "10.8.0.1:8096", "[::]:8096", "localhost:8096", "127.0.0.1",
               "127.0.0.1:0", "127.0.0.1:x"],
)  # fmt: skip
def test_auth_listen_must_be_loopback(listen: str) -> None:
    from pydantic import ValidationError

    from llmhub.configuration.weights_models import WeightsConfig

    with pytest.raises(ValidationError):
        WeightsConfig(auth_listen=listen)
