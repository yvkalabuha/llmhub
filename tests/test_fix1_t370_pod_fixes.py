"""T-370 fix round 1, pod side: F5, F7, F9 (front + supervisor), SEC-6, SEC-7, SEC-10."""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from collections import namedtuple
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from llmhub_infer import fetch as fetch_mod
from llmhub_infer import supervisor
from llmhub_infer.fetch import FetchError
from llmhub_infer.front import PodState, build_app
from pydantic import ValidationError
from starlette.testclient import TestClient

from i_fakes import FakeClock, FakeWeightsServer
from llmhub.configuration import weights_models
from llmhub.configuration.weights_models import InferConfig
from test_i_fetch import A_BASE, B_BASE, DRAFT, MODEL, check_done, make, server, src
from test_i_supervisor import KEY, Pod, base_env, fake_transport, health, running, until

Usage = namedtuple("Usage", "total used free")


# ------------------------------------------------------------------ F5: identity only
class _Gzipped(FakeWeightsServer):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        resp = super().handle_request(request)
        resp.headers["content-encoding"] = "gzip"
        return resp


class _BadStream(httpx.SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        raise httpx.DecodingError("broken coding")
        yield b""  # pragma: no cover


class _Decoding(FakeWeightsServer):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        super().handle_request(request)  # log the request
        return httpx.Response(200, headers={"content-length": "10"}, stream=_BadStream())


def test_f5_content_coded_response_fails_over(tmp_path: Path) -> None:
    c = FakeClock()
    a = _Gzipped(files=server(c, "/w/op/").files, clock=c)
    b = server(c, "/repo/resolve/rev/")
    env = make(tmp_path, {"a": a, "b": b}, [src("a", A_BASE), src("b", B_BASE)], c)
    check_done(env, env.run())
    assert env.events[-1].failovers[0] == {"source": "a", "file": "draft", "reason": "network"}
    assert len(a.requests_for("/w/op/d.gguf")) == 1  # no retry of a content-coding source


def test_f5_decoding_error_is_a_source_failure(tmp_path: Path) -> None:
    c = FakeClock()
    a = _Decoding(files=server(c, "/w/op/").files, clock=c)
    b = server(c, "/repo/resolve/rev/")
    env = make(tmp_path, {"a": a, "b": b}, [src("a", A_BASE), src("b", B_BASE)], c)
    check_done(env, env.run())
    assert env.events[-1].failovers[0]["reason"] == "network"
    assert len(a.requests_for("/w/op/d.gguf")) == 1


# ------------------------------------------------------------------ F7: disk on resume
def _sparse_resume(tmp_path: Path, done: list[int], chunk: int) -> None:
    (tmp_path / "model.gguf.part").write_bytes(b"\0" * len(MODEL))  # full-size (sparse) part
    (tmp_path / "model.gguf.part.json").write_text(
        json.dumps({"v": 1, "size": len(MODEL), "chunk": chunk, "done": done})
    )


def test_f7_resume_checks_disk_for_missing_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chunk = 32 * 1024
    _sparse_resume(tmp_path, [], chunk)  # nothing done yet: the whole model is missing
    c = FakeClock()
    s = server(c, "/w/op/")
    env = make(tmp_path, {"a": s}, [src("a", A_BASE, segments=4)], c, chunk=chunk)
    monkeypatch.setattr(fetch_mod.shutil, "disk_usage", lambda p: Usage(1, 1, len(DRAFT)))
    with pytest.raises(FetchError) as e:
        env.run()
    assert e.value.reason == "disk" and not s.requests_for("/w/op/m.gguf")


def test_f7_resume_needs_only_missing_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chunk = 32 * 1024
    n = -(-len(MODEL) // chunk)
    part = bytearray(MODEL)
    part[:chunk] = b"\0" * chunk
    (tmp_path / "model.gguf.part").write_bytes(bytes(part))
    (tmp_path / "model.gguf.part.json").write_text(
        json.dumps({"v": 1, "size": len(MODEL), "chunk": chunk, "done": list(range(1, n))})
    )
    c = FakeClock()
    s = server(c, "/w/op/")
    env = make(tmp_path, {"a": s}, [src("a", A_BASE, segments=4)], c, chunk=chunk)
    monkeypatch.setattr(fetch_mod.shutil, "disk_usage", lambda p: Usage(1, 1, len(DRAFT)))
    check_done(env, env.run())


# ------------------------------------------------------------------ F9: restart contract
def _client(st: PodState) -> TestClient:
    def h(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/health":
            return httpx.Response(503)
        return httpx.Response(200, stream=httpx.ByteStream(b"{}"))

    return TestClient(build_app(st, "KEY-1", upstream_transport=httpx.MockTransport(h)))


AUTH = {"Authorization": "Bearer KEY-1"}


def test_f9_front_reports_restarting_only_after_a_llama_exit() -> None:
    st = PodState()
    st.set_phase("llama_load")  # initial load
    c = _client(st)
    r = c.post("/v1/chat/completions", headers=AUTH, content=b"{}")
    assert r.status_code == 503 and r.json() == {"error": "not ready"}
    assert "restarting" not in c.get("/health", headers=AUTH).json()

    st.set_phase("ready")
    st.set_restarting()  # llama exited once, the single restart is running
    r = c.post("/v1/chat/completions", headers=AUTH, content=b"{}")
    assert r.status_code == 503 and r.json() == {"error": "restarting"}
    h = c.get("/health", headers=AUTH)
    assert h.status_code == 503
    assert h.json()["phase"] == "llama_load" and h.json()["restarting"] is True

    st.set_phase("ready")  # back up: the flag is cleared
    assert not st.is_restarting()
    assert c.post("/v1/chat/completions", headers=AUTH, content=b"{}").status_code == 200

    st.set_restarting()
    st.fail("llama_exited")  # second exit: failed wins
    assert not st.is_restarting()
    assert c.get("/health", headers=AUTH).status_code == 500
    r = c.post("/v1/chat/completions", headers=AUTH, content=b"{}")
    assert r.json() == {"error": "not ready"}


async def test_f9_supervisor_restart_is_reported(tmp_path: Path) -> None:
    env = base_env(FAKE_LLAMA_MODE="crash_once", FAKE_LLAMA_MARK=str(tmp_path / "mark"))
    pod = Pod(env, tmp_path, transport_factory=fake_transport())
    auth = {"Authorization": f"Bearer {KEY}"}
    async with httpx.AsyncClient(timeout=3) as c, running(pod):

        async def ok() -> bool:
            r = await health(pod, c)
            return r is not None and r.status_code == 200

        await until(ok)

        async def restarting() -> dict[str, Any] | None:
            r = await c.post(pod.url("/v1/chat/completions"), headers=auth, content=b"{}")
            return r.json() if r.status_code == 503 else None

        assert await until(restarting) == {"error": "restarting"}
        h = await health(pod, c)
        assert h is not None and h.json().get("restarting") is True
        await until(ok)  # the single restart succeeded
        r = await c.post(pod.url("/v1/chat/completions"), headers=auth, content=b"hi")
        assert r.status_code == 200 and r.json()["echo"] == "hi"


async def test_f9_cancel_during_spawn_leaves_no_orphan(monkeypatch: pytest.MonkeyPatch) -> None:
    procs: list[asyncio.subprocess.Process] = []
    real = asyncio.create_subprocess_exec

    async def spy(*a: Any, **kw: Any) -> asyncio.subprocess.Process:
        p = await real(*a, **kw)
        procs.append(p)
        return p

    monkeypatch.setattr(supervisor.asyncio, "create_subprocess_exec", spy)
    argv = [sys.executable, "-c", "import time; time.sleep(60)"]
    task = asyncio.create_task(supervisor._spawn(argv, {}))
    await asyncio.sleep(0)  # the spawn is in flight
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(procs) == 1 and procs[0].returncode is not None  # stopped, not orphaned


# ------------------------------------------------------------------ SEC-6: deny-list
def test_sec6_deny_lists_in_sync() -> None:
    assert supervisor._FORBIDDEN == weights_models.FORBIDDEN_LLAMA_FLAGS
    assert supervisor._FORBIDDEN_PREFIX == weights_models.FORBIDDEN_LLAMA_PREFIXES


@pytest.mark.parametrize(
    "flag",
    ["-hf", "--hf-repo", "-hfr", "--hf-file", "-hft", "--hf-token", "-hfd", "--hf-repo-draft",
     "--spec-draft-hf", "-mu", "--model-url", "-dr", "--docker-repo", "--spec-draft-model",
     "--path", "--api-prefix", "--slot-save-path", "--props", "--metrics", "--lora",
     "--lora-scaled", "--control-vector", "--control-vector-scaled", "-mm", "--mmproj", "-mmu",
     "--mmproj-url", "--rpc", "--log-file", "--log-prompts-dir", "-lcd", "--grammar-file",
     "-jf", "--chat-template-file", "--media-path", "--models-dir", "--models-preset",
     "--tools", "--tools-runtime", "--mcp-servers-json", "-ag", "--agent", "--connect",
     "--fim-qwen-7b-default", "--embd-gemma-default", "--ui-config-file", "--reuse-port",
     "--api-key-file", "--ssl-key-file"],
)  # fmt: skip
def test_sec6_flags_rejected_in_config_and_pod(flag: str) -> None:
    with pytest.raises(supervisor.EnvError):
        supervisor.check_llama_args(["-ngl", "9", flag, "x"])
    with pytest.raises(supervisor.EnvError):
        supervisor.check_llama_args([f"{flag}=x"])
    with pytest.raises(ValidationError):
        InferConfig(llama_args=["-ngl", "9", flag, "x"])


def test_sec6_defaults_and_harmless_flags_pass() -> None:
    args = [*supervisor.DEFAULT_LLAMA_ARGS, "--no-mmproj", "--mmproj-offload", "--no-webui"]
    assert supervisor.check_llama_args(args) == args
    InferConfig(llama_args=args)


# ------------------------------------------------------------------ SEC-7: path allow-list
def _seen_front() -> tuple[TestClient, list[tuple[str, bytes]]]:
    seen: list[tuple[str, bytes]] = []

    def h(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.raw_path))
        return httpx.Response(200, stream=httpx.ByteStream(b"{}"))

    st = PodState()
    st.set_phase("ready")
    return TestClient(build_app(st, "KEY-1", upstream_transport=httpx.MockTransport(h))), seen


@pytest.mark.parametrize(
    "path",
    ["/v1/slots", "/v1/../slots", "/v1/%2e/chat/completions", "/v1//chat/completions",
     "/v1/chat/completions/", "/v1/chat%2Fcompletions", "/v1/chat%5Ccompletions",
     "/v1/%252e%252e/slots", "/v1/metrics", "/v1/lora-adapters"],
)  # fmt: skip
def test_sec7_unknown_or_unnormalized_paths_404(path: str) -> None:
    c, seen = _seen_front()
    r = c.post(path, headers=AUTH, content=b"{}")
    assert r.status_code == 404 and not seen


def test_sec7_unauthorized_still_401_first() -> None:
    c, seen = _seen_front()
    assert c.post("/v1/%2e%2e/slots", content=b"{}").status_code == 401 and not seen


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/v1/chat/completions"), ("POST", "/v1/completions"),
     ("POST", "/v1/embeddings"), ("GET", "/v1/models"), ("GET", "/props")],
)  # fmt: skip
def test_sec7_allowed_paths_forwarded(method: str, path: str) -> None:
    c, seen = _seen_front()
    r = c.request(method, path + "?x=1", headers=AUTH, content=b"{}")
    assert r.status_code == 200 and seen == [(method, path.encode() + b"?x=1")]


# ------------------------------------------------------------------ SEC-10: tmpfs
def test_sec10_tls_files_default_to_tmpfs() -> None:
    assert supervisor.DEFAULT_RUN_DIR == Path("/dev/shm/llmhub")
    assert inspect.signature(supervisor.main).parameters["run_dir"].default == Path(
        "/dev/shm/llmhub"
    )
