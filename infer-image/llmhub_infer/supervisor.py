"""Pod supervisor: env -> front up -> weights -> llama-server (127.0.0.1 only) -> READY."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx
import uvicorn

from .fetch import Fetcher, FetchError, Progress
from .front import PodState, build_app
from .plan import PlanError, WeightsPlan, parse_plan

LLAMA_HOST = "127.0.0.1"
LLAMA_PORT = 8081
DEFAULT_RUN_DIR = Path("/dev/shm/llmhub")  # tmpfs: the TLS key stays in RAM
DEFAULT_LLAMA_ARGS = [
    "-ngl", "999", "-fa", "on", "-c", "65536", "-np", "1", "--jinja",
    "--spec-type", "draft-mtp", "--spec-draft-n-max", "3",
]  # fmt: skip
# Set by the pod supervisor, or loading models/files, writing files, opening network paths or
# exposing extra endpoints (checked against llama-server b12063 --help). Same list in the pod
# (`llmhub.configuration.weights_models`); a test keeps both in sync.
_FORBIDDEN = frozenset({
    "-m", "--model", "-md", "--model-draft", "--spec-draft-model", "--host", "--port",
    "--reuse-port", "--path", "--api-prefix",
    "-mu", "--model-url", "-dr", "--docker-repo", "-hf", "-hfr", "-hff", "-hft", "-hfd", "-hfrd",
    "-mm", "--mmproj", "-mmu", "--mmproj-url", "--rpc", "--embd-gemma-default",
    "-lcs", "--lookup-cache-static", "-lcd", "--lookup-cache-dynamic",
    "--grammar-file", "-jf", "--json-schema-file", "--chat-template-file",
    "--log-file", "--slot-save-path", "--media-path", "--props", "--metrics",
    "-ag", "--agent", "--ui-config-file", "--webui-config-file", "--ui-mcp-proxy",
    "--webui-mcp-proxy", "--video-ffmpeg-dir",
})  # fmt: skip
_FORBIDDEN_PREFIX = (
    "--api-key", "--ssl-", "--hf-", "--spec-draft-hf", "--lora", "--control-vector",
    "--models-", "--log-prompts", "--tools", "--mcp-", "--connect", "--fim-",
)  # fmt: skip
_SECRET_NAME = re.compile(r"(TOKEN|SECRET|PASSWORD|API_?KEY|PRIVATE|CREDENTIAL)", re.IGNORECASE)
_OP_RE = re.compile(r"^[0-9a-f]{12}$")


class EnvError(ValueError):
    """Bad pod environment. Messages name variables, never values."""


@dataclass(frozen=True)
class PodEnv:
    op_id: str
    transport: str
    port: int
    plan: WeightsPlan
    llama_args: list[str]
    api_key: str
    weights_token: str | None
    tls_cert_pem: str | None
    tls_key_pem: str | None
    hf_token: str | None
    weights_dir: Path
    llama_bin: str = "/app/llama-server"

    def __repr__(self) -> str:  # never print secrets
        return f"PodEnv(op_id={self.op_id!r}, transport={self.transport!r}, port={self.port})"


def _forbidden(arg: str) -> bool:
    name = arg.split("=", 1)[0]
    return name in _FORBIDDEN or name.startswith(_FORBIDDEN_PREFIX)


def check_llama_args(args: object) -> list[str]:
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise EnvError("LLMHUB_LLAMA_ARGS: must be a JSON list of strings")
    if any(_forbidden(a) for a in args):
        raise EnvError("LLMHUB_LLAMA_ARGS: contains a forbidden (pod-owned or unsafe) flag")
    return list(args)


def read_env(env: Mapping[str, str]) -> PodEnv:
    def need(name: str) -> str:
        v = env.get(name, "")
        if not v:
            raise EnvError(f"{name}: required")
        return v

    op_id = need("LLMHUB_OP_ID")
    if not _OP_RE.match(op_id):
        raise EnvError("LLMHUB_OP_ID: expected 12 hex characters")
    transport = need("LLMHUB_TRANSPORT")
    if transport not in ("direct", "proxy"):
        raise EnvError("LLMHUB_TRANSPORT: must be direct or proxy")
    raw_port = env.get("LLMHUB_PORT") or ("8443" if transport == "direct" else "8080")
    if not raw_port.isdigit() or not 0 < int(raw_port) < 65536:
        raise EnvError("LLMHUB_PORT: must be a port number")
    try:
        plan = parse_plan(need("LLMHUB_WEIGHTS_PLAN"))
    except PlanError as e:
        raise EnvError(f"LLMHUB_WEIGHTS_PLAN: {e}") from None
    raw_args = env.get("LLMHUB_LLAMA_ARGS")
    try:
        args = json.loads(raw_args) if raw_args else list(DEFAULT_LLAMA_ARGS)
    except ValueError:
        raise EnvError("LLMHUB_LLAMA_ARGS: not valid JSON") from None
    api_key = need("LLMHUB_API_KEY")
    token = env.get("LLMHUB_WEIGHTS_TOKEN") or None
    if token is None and any(s.auth == "weights_token" for s in plan.sources):
        raise EnvError("LLMHUB_WEIGHTS_TOKEN: required by a plan source")
    cert = env.get("LLMHUB_TLS_CERT_PEM") or None
    key = env.get("LLMHUB_TLS_KEY_PEM") or None
    if transport == "direct" and (cert is None or key is None):
        raise EnvError("LLMHUB_TLS_CERT_PEM / LLMHUB_TLS_KEY_PEM: required for direct")
    return PodEnv(
        op_id=op_id,
        transport=transport,
        port=int(raw_port),
        plan=plan,
        llama_args=check_llama_args(args),
        api_key=api_key,
        weights_token=token,
        tls_cert_pem=cert if transport == "direct" else None,
        tls_key_pem=key if transport == "direct" else None,
        hf_token=env.get("HF_TOKEN") or None,
        weights_dir=Path(env.get("LLMHUB_WEIGHTS_DIR") or "/models"),
    )


def llama_argv(pe: PodEnv, files: Mapping[str, Path], port: int = LLAMA_PORT) -> list[str]:
    argv = [pe.llama_bin, *pe.llama_args, "-m", str(files["model"])]
    if "draft" in files:
        argv += ["-md", str(files["draft"])]
    return [*argv, "--host", LLAMA_HOST, "--port", str(port)]


def child_env(env: Mapping[str, str]) -> dict[str, str]:
    """Environment for llama-server: no LLMHUB_*, no HF_TOKEN, nothing that looks like a secret,
    no LLAMA_ARG_* (the base image sets LLAMA_ARG_HOST/PORT; only our argv configures llama)."""
    return {
        k: v
        for k, v in env.items()
        if not k.startswith(("LLMHUB_", "LLAMA_ARG_")) and not _SECRET_NAME.search(k)
    }


def write_tls_files(pe: PodEnv, run_dir: Path) -> tuple[Path, Path]:
    """Cert and key into a 0700 directory, files 0600.

    The default `run_dir` (`DEFAULT_RUN_DIR`, /dev/shm/llmhub) is tmpfs in Docker/RunPod
    containers, so the private key never touches the rented host's disk.
    """
    assert pe.tls_cert_pem is not None and pe.tls_key_pem is not None
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    run_dir.chmod(0o700)
    out = []
    for name, data in (("tls.crt", pe.tls_cert_pem), ("tls.key", pe.tls_key_pem)):
        path = run_dir / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(data)
        path.chmod(0o600)
        out.append(path)
    return out[0], out[1]


class _Server(uvicorn.Server):
    """uvicorn without its own signal handling: the supervisor owns SIGTERM."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def _fetch_in_thread(
    fetcher: Fetcher, loop: asyncio.AbstractEventLoop
) -> asyncio.Future[dict[str, Path]]:
    fut: asyncio.Future[dict[str, Path]] = loop.create_future()

    def done(res: dict[str, Path] | BaseException) -> None:
        if fut.done():
            return
        if isinstance(res, BaseException):
            fut.set_exception(res)
        else:
            fut.set_result(res)

    def work() -> None:
        try:
            res: dict[str, Path] | BaseException = fetcher.run()
        except BaseException as e:  # noqa: BLE001 - relayed to the loop
            res = e
        loop.call_soon_threadsafe(done, res)

    threading.Thread(target=work, name="weights-fetch", daemon=True).start()
    return fut


async def _llama_loop(
    pe: PodEnv,
    files: Mapping[str, Path],
    state: PodState,
    env: Mapping[str, str],
    llama_bin: str | None,
    port: int,
    poll_s: float,
) -> None:
    """Run llama-server; one restart allowed, the second exit marks the pod failed.

    After the first exit the pod reports ``restarting`` (front: 503 {"error": "restarting"}),
    so the controller treats in-flight jobs as a bounded network loss, not a lost instance.
    """
    pod = pe if llama_bin is None else PodEnv(**{**pe.__dict__, "llama_bin": llama_bin})
    argv = llama_argv(pod, files, port)
    exits = 0
    async with httpx.AsyncClient(timeout=2.0) as http:
        while True:
            if exits == 0:
                state.set_phase("llama_load")
            else:
                state.set_restarting()
            try:
                proc = await _spawn(argv, child_env(env))
            except OSError:  # binary missing / not executable: counts as an exit
                exits += 1
                if exits >= 2:
                    state.fail("llama_exited")
                    return
                await asyncio.sleep(poll_s)
                continue
            try:
                waiter = asyncio.ensure_future(proc.wait())
                while not waiter.done():
                    try:
                        r = await http.get(f"http://{LLAMA_HOST}:{port}/health")
                        if r.status_code == 200:
                            state.set_phase("ready")
                    except httpx.HTTPError:
                        pass
                    await asyncio.wait({waiter}, timeout=poll_s)
            except asyncio.CancelledError:
                await _stop_child(proc)
                raise
            exits += 1
            if exits >= 2:
                state.fail("llama_exited")
                return


async def _spawn(argv: list[str], env: Mapping[str, str]) -> asyncio.subprocess.Process:
    """Start llama-server; a cancellation during the spawn still stops the new child (no orphan)."""
    spawn = asyncio.ensure_future(
        asyncio.create_subprocess_exec(*argv, env=dict(env), stdin=asyncio.subprocess.DEVNULL)
    )
    try:
        return await asyncio.shield(spawn)
    except asyncio.CancelledError:
        try:
            proc = await asyncio.shield(spawn)
        except asyncio.CancelledError:  # cancelled again: kill the child once it exists
            spawn.add_done_callback(_kill_spawned)
            raise
        except Exception:  # noqa: BLE001 - the spawn failed: nothing to stop
            raise asyncio.CancelledError from None
        await _stop_child(proc)
        raise


def _kill_spawned(fut: asyncio.Future[asyncio.subprocess.Process]) -> None:
    if fut.cancelled() or fut.exception() is not None:
        return
    proc = fut.result()
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


async def _stop_child(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), 10)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()


async def _orchestrate(
    pe: PodEnv,
    state: PodState,
    env: Mapping[str, str],
    llama_bin: str | None,
    transport_factory: Callable[..., httpx.BaseTransport] | None,
    wall: Callable[[], float],
    llama_port: int,
    poll_s: float,
) -> None:
    loop = asyncio.get_running_loop()
    secrets = {
        k: v
        for k, v in (("LLMHUB_WEIGHTS_TOKEN", pe.weights_token), ("HF_TOKEN", pe.hf_token))
        if v
    }

    def on_progress(p: Progress) -> None:
        state.set_progress(p)

    fetcher = Fetcher(
        pe.plan, pe.weights_dir, secrets, on_progress, transport_factory=transport_factory
    )
    fut = _fetch_in_thread(fetcher, loop)
    deadline = pe.plan.deadline_unix
    timeout = None if deadline is None else max(0.0, deadline - wall()) + 5.0
    try:
        files = await asyncio.wait_for(asyncio.shield(fut), timeout)
    except TimeoutError:
        state.fail("deadline")
        return
    except FetchError as e:
        state.fail(e.reason)
        return
    except Exception:  # noqa: BLE001 - never leak details; the pod stays up reporting failure
        state.fail("internal")
        return
    try:
        await _llama_loop(pe, files, state, env, llama_bin, llama_port, poll_s)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the pod stays up and reports failure
        state.fail("internal")


async def main(
    env: Mapping[str, str] | None = None,
    *,
    llama_bin: str | None = None,
    transport_factory: Callable[..., httpx.BaseTransport] | None = None,
    run_dir: Path = DEFAULT_RUN_DIR,
    bind_host: str = "0.0.0.0",
    llama_port: int = LLAMA_PORT,
    poll_s: float = 0.2,
    wall: Callable[[], float] = time.time,
    stop: asyncio.Event | None = None,
) -> int:
    env = os.environ if env is None else env
    try:
        pe = read_env(env)
    except EnvError as e:
        print(f"llmhub-infer: bad environment: {e}", file=sys.stderr)
        return 2
    state = PodState(source_names=tuple(s.name for s in pe.plan.sources))
    app = build_app(state, pe.api_key, f"http://{LLAMA_HOST}:{llama_port}")
    cfg: dict[str, object] = {}
    if pe.transport == "direct":
        cert, key = write_tls_files(pe, run_dir)
        cfg = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
    server = _Server(
        uvicorn.Config(app, host=bind_host, port=pe.port, log_level="warning", **cfg)  # type: ignore[arg-type]
    )
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, stop.set)
    serve = asyncio.create_task(server.serve())
    work = asyncio.create_task(
        _orchestrate(pe, state, env, llama_bin, transport_factory, wall, llama_port, poll_s)
    )
    stopper = asyncio.create_task(stop.wait())
    await asyncio.wait({serve, stopper}, return_when=asyncio.FIRST_COMPLETED)
    work.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await work
    server.should_exit = True
    stopper.cancel()
    with contextlib.suppress(Exception):
        await serve
    return 0
