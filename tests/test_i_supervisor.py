"""T-370 stream C2/C3: pod env parsing, llama argv, secret stripping, supervisor integration."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import ssl
import stat
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from llmhub_infer import supervisor
from llmhub_infer.supervisor import (
    EnvError,
    child_env,
    llama_argv,
    read_env,
    write_tls_files,
)

from i_fakes import FakeClock, FakeWeightsServer, make_plan_json
from tls_certs import make_self_signed

KEY = "api-key-xyz"
FAKE = str(Path(__file__).parent / "i_llama_fake.py")
FILES = {"draft": b"d" * 4096, "model": b"m" * 8192}
SRC = {
    "name": "self_hosted",
    "base_url": "https://w.example/w/op/",
    "paths": {"model": "model.gguf", "draft": "draft.gguf"},
}


def port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def plan_json(**kw: Any) -> str:
    return make_plan_json(FILES, [{**SRC, "auth": "none"}], **kw)


def base_env(**over: str) -> dict[str, str]:
    env = {
        "LLMHUB_OP_ID": "0123456789ab",
        "LLMHUB_TRANSPORT": "proxy",
        "LLMHUB_PORT": "8080",
        "LLMHUB_WEIGHTS_PLAN": plan_json(),
        "LLMHUB_API_KEY": KEY,
    }
    env.update(over)
    return env


# ------------------------------------------------------------------ C2 units
def test_read_env_ok_and_defaults() -> None:
    pe = read_env(base_env())
    assert pe.transport == "proxy" and pe.port == 8080 and pe.weights_dir == Path("/models")
    assert "draft-mtp" in pe.llama_args and "--spec-draft-n-max" in pe.llama_args
    assert KEY not in repr(pe)


@pytest.mark.parametrize(
    "drop,over",
    [
        ("LLMHUB_API_KEY", {}),
        ("LLMHUB_OP_ID", {}),
        (None, {"LLMHUB_TRANSPORT": "weird"}),
        (None, {"LLMHUB_TRANSPORT": "direct"}),  # no certificate
        (None, {"LLMHUB_WEIGHTS_PLAN": "not json"}),
        (None, {"LLMHUB_LLAMA_ARGS": "not json"}),
    ],
)
def test_read_env_errors_without_values(drop: str | None, over: dict[str, str]) -> None:
    env = base_env(LLMHUB_WEIGHTS_TOKEN="tok-secret", **over)
    if drop:
        del env[drop]
    with pytest.raises(EnvError) as ei:
        read_env(env)
    assert KEY not in str(ei.value) and "tok-secret" not in str(ei.value)


def test_read_env_requires_weights_token_for_token_source() -> None:
    plan = make_plan_json(FILES, [{**SRC, "auth": "weights_token"}])
    with pytest.raises(EnvError):
        read_env(base_env(LLMHUB_WEIGHTS_PLAN=plan))
    assert read_env(base_env(LLMHUB_WEIGHTS_PLAN=plan, LLMHUB_WEIGHTS_TOKEN="t")).weights_token


@pytest.mark.parametrize(
    "bad",
    [["-m", "x"], ["--model", "x"], ["-md", "x"], ["--model-draft=x"], ["--host", "0.0.0.0"],
     ["--port=1"], ["--api-key", "k"], ["--api-key-file", "f"], ["--ssl-key-file", "k"]],
)  # fmt: skip
def test_forbidden_llama_args(bad: list[str]) -> None:
    with pytest.raises(EnvError):
        read_env(base_env(LLMHUB_LLAMA_ARGS=json.dumps(bad)))


def test_llama_argv_layout() -> None:
    pe = read_env(
        base_env(LLMHUB_LLAMA_ARGS=json.dumps(["-ngl", "999", "--spec-type", "draft-mtp"]))
    )
    argv = llama_argv(
        pe, {"model": Path("/models/model.gguf"), "draft": Path("/models/draft.gguf")}
    )
    assert argv == [
        "/app/llama-server", "-ngl", "999", "--spec-type", "draft-mtp",
        "-m", "/models/model.gguf", "-md", "/models/draft.gguf",
        "--host", "127.0.0.1", "--port", "8081",
    ]  # fmt: skip


def test_child_env_strips_secrets() -> None:
    env = {
        "PATH": "/usr/bin", "LANG": "C", "LLMHUB_API_KEY": "a", "LLMHUB_WEIGHTS_TOKEN": "b",
        "LLMHUB_TLS_KEY_PEM": "c", "LLMHUB_TLS_CERT_PEM": "d", "HF_TOKEN": "e",
        "RUNPOD_API_KEY": "f", "LLMHUB_OP_ID": "g", "CUDA_VISIBLE_DEVICES": "0",
    }  # fmt: skip
    assert child_env(env) == {"PATH": "/usr/bin", "LANG": "C", "CUDA_VISIBLE_DEVICES": "0"}


def test_tls_files_0600(tmp_path: Path) -> None:
    b = make_self_signed()
    pe = read_env(
        base_env(
            LLMHUB_TRANSPORT="direct", LLMHUB_TLS_CERT_PEM=b.cert_pem, LLMHUB_TLS_KEY_PEM=b.key_pem
        )
    )
    cert, key = write_tls_files(pe, tmp_path / "run")
    for f in (cert, key):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "run").stat().st_mode) == 0o700
    assert key.read_text() == b.key_pem


# ------------------------------------------------------------ C3 integration
class Pod:
    def __init__(self, env: dict[str, str], tmp: Path, **kw: Any) -> None:
        self.p = port()
        self.lp = port()
        self.env = {**env, "LLMHUB_PORT": str(self.p), "LLMHUB_WEIGHTS_DIR": str(tmp / "models")}
        self.stop = asyncio.Event()
        self.kw = kw
        self.tmp = tmp
        self.task: asyncio.Task[int] | None = None

    def start(self) -> None:
        kw: dict[str, Any] = {
            "llama_bin": FAKE, "bind_host": "127.0.0.1", "llama_port": self.lp, "poll_s": 0.05,
            "run_dir": self.tmp / "run", "stop": self.stop,
        }  # fmt: skip
        kw.update(self.kw)
        self.task = asyncio.create_task(supervisor.main(self.env, **kw))

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.p}{path}"


def fake_transport() -> Callable[..., httpx.BaseTransport]:
    srv = FakeWeightsServer(
        files={f"/w/op/{k}.gguf": v for k, v in FILES.items()}, clock=FakeClock()
    )
    return lambda _src: srv


async def until(pred: Callable[[], Any], timeout: float = 10.0) -> Any:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        r = await pred()
        if r:
            return r
        await asyncio.sleep(0.05)
    raise AssertionError("timeout")


@contextlib.asynccontextmanager
async def running(pod: Pod) -> AsyncIterator[Pod]:
    pod.start()
    try:
        yield pod
    finally:
        pod.stop.set()
        assert pod.task is not None
        await asyncio.wait_for(pod.task, 15)


async def health(pod: Pod, client: httpx.AsyncClient) -> httpx.Response | None:
    try:
        return await client.get(pod.url("/health"), headers={"Authorization": f"Bearer {KEY}"})
    except httpx.HTTPError:
        return None


async def test_weights_then_llama_then_ready_and_sigterm_path(tmp_path: Path) -> None:
    env = base_env(
        LLMHUB_WEIGHTS_PLAN=plan_json(),
        HF_TOKEN="hf-secret",
        LLMHUB_TLS_KEY_PEM="x",
        FAKE_LLAMA_MODE="serve",
    )
    pod = Pod(env, tmp_path, transport_factory=fake_transport())
    async with httpx.AsyncClient(timeout=3) as c:
        async with running(pod):

            async def ok() -> bool:
                r = await health(pod, c)
                return r is not None and r.status_code == 200

            await until(ok)
            assert (tmp_path / "models" / "model.gguf").read_bytes() == FILES["model"]
            llama = f"http://127.0.0.1:{pod.lp}"
            keys = (await c.get(f"{llama}/env")).json()
            assert not [k for k in keys if k.startswith("LLMHUB_") or k == "HF_TOKEN"]
            argv = (await c.get(f"{llama}/argv")).json()
            assert argv[argv.index("--host") + 1] == "127.0.0.1"
            assert "-md" in argv and "--api-key" not in argv
            r = await c.post(
                pod.url("/v1/echo"), headers={"Authorization": f"Bearer {KEY}"}, content=b"hi"
            )
            assert r.json()["echo"] == "hi" and r.json()["auth"] is None
            pid = (await c.get(f"{llama}/pid")).json()
        assert pod.task is not None and pod.task.result() == 0
    with pytest.raises(ProcessLookupError):
        await asyncio.sleep(0.2)
        os.kill(pid, 0)


async def test_llama_exits_twice_then_failed_but_alive(tmp_path: Path) -> None:
    pod = Pod(base_env(FAKE_LLAMA_MODE="exit"), tmp_path, transport_factory=fake_transport())
    async with httpx.AsyncClient(timeout=3) as c, running(pod):

        async def failed() -> httpx.Response | None:
            r = await health(pod, c)
            return r if r is not None and r.status_code == 500 else None

        r = await until(failed)
        assert r.json() == {"status": "failed", "reason": "llama_exited"}
        await asyncio.sleep(0.3)
        assert pod.task is not None and not pod.task.done()  # no exit, no restart loop


async def test_weights_exhausted_failed_but_alive(tmp_path: Path) -> None:
    srv = FakeWeightsServer(files={}, clock=FakeClock())  # every path 404
    pod = Pod(base_env(), tmp_path, transport_factory=lambda _s: srv)
    async with httpx.AsyncClient(timeout=3) as c, running(pod):

        async def failed() -> httpx.Response | None:
            r = await health(pod, c)
            return r if r is not None and r.status_code == 500 else None

        r = await until(failed)
        assert r.json()["status"] == "failed"
        await asyncio.sleep(0.3)
        assert pod.task is not None and not pod.task.done()
        assert (await c.get(pod.url("/ping"))).status_code == 204


async def test_deadline_failed_but_alive_C3b(tmp_path: Path) -> None:
    plan = plan_json(deadline_unix=time.time() - 5)
    pod = Pod(base_env(LLMHUB_WEIGHTS_PLAN=plan), tmp_path, transport_factory=fake_transport())
    async with httpx.AsyncClient(timeout=3) as c, running(pod):

        async def failed() -> httpx.Response | None:
            r = await health(pod, c)
            return r if r is not None and r.status_code == 500 else None

        r = await until(failed)
        assert r.json() == {"status": "failed", "reason": "deadline"}
        await asyncio.sleep(0.5)
        assert pod.task is not None and not pod.task.done()
        r2 = await health(pod, c)
        assert r2 is not None and r2.status_code == 500  # stays failed, no restart loop


async def test_missing_llama_binary_fails_but_alive(tmp_path: Path) -> None:
    pod = Pod(base_env(), tmp_path, transport_factory=fake_transport(), llama_bin="/nonexistent")
    async with httpx.AsyncClient(timeout=3) as c, running(pod):

        async def failed() -> httpx.Response | None:
            r = await health(pod, c)
            return r if r is not None and r.status_code == 500 else None

        r = await until(failed)
        assert r.json()["reason"] == "llama_exited"


async def test_direct_mode_pinned_cert(tmp_path: Path) -> None:
    b = make_self_signed()
    env = base_env(
        LLMHUB_TRANSPORT="direct", LLMHUB_TLS_CERT_PEM=b.cert_pem, LLMHUB_TLS_KEY_PEM=b.key_pem
    )
    pod = Pod(env, tmp_path, transport_factory=fake_transport())
    good = ssl.create_default_context(cadata=b.cert_pem)
    other = ssl.create_default_context(cadata=make_self_signed().cert_pem)
    async with running(pod):

        async def up() -> bool:
            try:
                async with httpx.AsyncClient(verify=good) as c:
                    return (await c.get(f"https://127.0.0.1:{pod.p}/ping")).status_code == 204
            except httpx.HTTPError:
                return False

        await until(up)
        with pytest.raises(httpx.ConnectError):
            async with httpx.AsyncClient(verify=other) as c:
                await c.get(f"https://127.0.0.1:{pod.p}/ping")


def test_bad_env_exit_code_and_sigterm(tmp_path: Path) -> None:
    code = "import asyncio,sys; from llmhub_infer.supervisor import main; sys.exit(asyncio.run(main()))"
    root = Path(__file__).resolve().parents[1] / "infer-image"
    penv = {"PATH": os.environ["PATH"], "PYTHONPATH": str(root)}
    r = subprocess.run(
        [sys.executable, "-c", code], env=penv, check=False, capture_output=True, text=True
    )
    assert r.returncode == 2 and "LLMHUB_" in r.stderr and "Traceback" not in r.stderr
    p = port()
    env = {
        **penv, **base_env(LLMHUB_PORT=str(p), LLMHUB_WEIGHTS_DIR=str(tmp_path / "m")),
        "LLMHUB_WEIGHTS_PLAN": make_plan_json(
            FILES, [{**SRC, "base_url": "https://127.0.0.1:1/"}], retries_per_source=0
        ),
    }  # fmt: skip
    proc = subprocess.Popen([sys.executable, "-c", code], env=env, stderr=subprocess.PIPE)
    try:
        end = time.monotonic() + 15
        while time.monotonic() < end:
            try:
                if httpx.get(f"http://127.0.0.1:{p}/ping").status_code == 204:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            raise AssertionError("front never came up")
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(15) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
