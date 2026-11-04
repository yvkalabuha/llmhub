"""Composition root of the API process (T-369 stream I).

One process, one asyncio loop, one SQLite connection: the `Controller` (cloud plane), the
`LocalWorker` (local llama-servers, same `boot_id`), the shared `JobWaiter` and the FastAPI
gateway all live on the same loop and use the same connection. The reaper stays a separate
process (`python -m llmhub.reaper`) with its own connection and adapters.

Providers: only `mock` has an adapter (real adapters arrive with T-372). Mock pools use
`MockInference`; any other provider gets the shared `HttpInferenceBackend` (T-370) whose
`on_timings` feeds the per-pool speed EWMA and the MTP acceptance EWMA in `controller_state`.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

import httpx

from llmhub import __version__
from llmhub.api_gateway.app import Runtime, create_app
from llmhub.api_gateway.auth import ClientRegistry, install_sighup
from llmhub.api_gateway.waiter import JobWaiter
from llmhub.budget_manager.estimate import update_speed_ewma
from llmhub.clock import Clock
from llmhub.cloud_manager.http_inference import HttpInferenceBackend, Timings, make_cloud_inference
from llmhub.cloud_manager.inference import InferenceBackend
from llmhub.cloud_manager.instance_env import InstanceSecrets, MaterialFn, make_material_fns
from llmhub.configuration.loader import (
    ConfigError,
    load_instance_secret,
    load_label_secret,
    load_weights_secret,
)
from llmhub.configuration.models import Config
from llmhub.controller import Controller, new_boot_id
from llmhub.cost_tracker.txutil import join_or_open, state_get, state_set
from llmhub.local_inference.client import LocalBackendClient, api_key_from_env
from llmhub.local_inference.worker import LocalWorker
from llmhub.model_router.views import CloudState
from llmhub.persistence.integrity import open_state_db
from llmhub.provider_adapters.base import ProviderAdapter
from llmhub.provider_adapters.mock import MockProvider
from llmhub.provider_adapters.mock_inference import MockInference
from llmhub.provider_adapters.mock_store import MockStore
from llmhub.task_queue.bodies import BodyStore

log = logging.getLogger("llmhub.server")

MOCK_STORE_ENV = "LLMHUB_MOCK_STORE"
SPEED_ALPHA = 0.3


def mtp_key(pool: str) -> str:
    return f"ewma:{pool}:mtp_accept"


def make_speed_consumer(
    conn: sqlite3.Connection, cfg: Config, clock: Clock
) -> Callable[[str, Timings], None]:
    """`on_timings` of the cloud backend: llama-server timings -> per-pool EWMA of pp/tg
    (read by router, launch estimate, Retry-After) and of the MTP draft acceptance rate."""

    def consume(job_id: str, t: Timings) -> None:
        row = conn.execute("SELECT pool FROM jobs WHERE id=?", (job_id,)).fetchone()
        pool = None if row is None else row["pool"]
        pc = cfg.pools.get(pool) if pool else None
        if pc is None:
            return
        now = clock.now()
        update_speed_ewma(
            conn, pc.name, t.pp_tps or None, t.tg_tps or None, SPEED_ALPHA,
            now=now, pp0=pc.pp_tps, tg0=pc.tg_tps,
        )  # fmt: skip
        rate = t.accept_rate
        if rate is not None:
            with join_or_open(conn):
                prev = state_get(conn, mtp_key(pc.name))
                value = rate if not prev else SPEED_ALPHA * rate + (1 - SPEED_ALPHA) * float(prev)
                state_set(conn, mtp_key(pc.name), repr(value), now)

    return consume


def build_adapters(cfg: Config, env: Mapping[str, str], clock: Clock) -> dict[str, ProviderAdapter]:
    """Adapters of `providers.enabled`. Only `mock` exists (store shared with the reaper:
    `<data_dir>/mock_cloud.db`, override `LLMHUB_MOCK_STORE`)."""
    adapters: dict[str, ProviderAdapter] = {}
    for name in cfg.providers.enabled:
        if name != "mock":
            raise ConfigError(f"provider {name!r} has no adapter yet (T-372)")
        store = env.get(MOCK_STORE_ENV) or str(cfg.data_dir / "mock_cloud.db")
        adapters[name] = MockProvider(MockStore(store), clock)
    return adapters


def load_instance_secrets(cfg: Config, env: Mapping[str, str]) -> InstanceSecrets:
    hf = env.get(cfg.weights.hf_token_env) if cfg.weights.hf_token_env else None
    return InstanceSecrets(
        instance=load_instance_secret(env), weights=load_weights_secret(env), hf_token=hf or None
    )


@dataclass
class Service:
    """Everything the process owns; `aclose` releases network clients (not job rows)."""

    rt: Runtime
    backends: dict[str, LocalBackendClient]
    http_inference: HttpInferenceBackend | None
    safe_mode: bool

    async def aclose(self) -> None:
        for b in self.backends.values():
            try:
                await b.aclose()
            except Exception:  # pragma: no cover - best effort on shutdown
                log.exception("closing local backend %s failed", b.name)
        if self.http_inference is not None:
            await self.http_inference.aclose()


def build_service(
    cfg: Config,
    env: Mapping[str, str],
    clock: Clock,
    *,
    config_path: Path | None = None,
    adapters: Mapping[str, ProviderAdapter] | None = None,
    inference: Mapping[str, InferenceBackend] | None = None,
    local_http: Mapping[str, httpx.AsyncClient] | None = None,
    instance_secrets: InstanceSecrets | None = None,
    boot_id: str | None = None,
) -> Service:
    """open_state_db -> clients sync -> adapters/inference -> Controller -> LocalWorker.

    Keyword overrides are test seams (FakeClock, ASGI fakes, Mock knobs); production passes
    none of them.
    """
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    opened = open_state_db(cfg.data_dir / "state.db", clock)
    conn = opened.conn
    conn.execute(f"PRAGMA busy_timeout={int(cfg.db.busy_timeout_ms)}")
    if opened.safe_mode:
        log.critical("state.db opened in safe mode (%s): HALTED", opened.reason)
    secret = load_label_secret(env)
    bodies = BodyStore(cfg.data_dir / "bodies")

    adapters = dict(adapters) if adapters is not None else build_adapters(cfg, env, clock)
    needs_http = [p for p in adapters if not isinstance(adapters[p], MockProvider)]
    has_weights = any(p.weights is not None for p in cfg.pools.values())
    secrets = instance_secrets
    if secrets is None and (has_weights or (needs_http and inference is None)):
        secrets = load_instance_secrets(cfg, env)
    http_backend: HttpInferenceBackend | None = None
    if inference is None:
        inf: dict[str, InferenceBackend] = {}
        for name, a in adapters.items():
            if isinstance(a, MockProvider):
                inf[name] = MockInference(a, clock)
            else:
                assert secrets is not None
                if http_backend is None:
                    http_backend = make_cloud_inference(
                        conn, secrets, cfg, clock,
                        on_timings=make_speed_consumer(conn, cfg, clock),
                    )  # fmt: skip
                inf[name] = http_backend
        inference = inf
    materials: dict[str, MaterialFn] = {}
    if has_weights:
        assert secrets is not None
        materials = make_material_fns(cfg, secrets)

    controller = Controller(
        conn, cfg, clock, adapters, inference, bodies, secret, boot_id or new_boot_id(),
        materials=materials,
    )  # fmt: skip
    waiter = JobWaiter(conn, clock, cfg.api.wait_poll_s)

    backends: dict[str, LocalBackendClient] = {}
    for name, bc in cfg.local_backends.items():
        try:
            key = api_key_from_env(bc, env)
        except ValueError:
            log.warning("local backend %s: %s is not set; calling without a key",
                        name, bc.api_key_env)  # fmt: skip
            key = None
        http = None if local_http is None else local_http.get(name)
        backends[name] = LocalBackendClient(
            name, bc, key, http=http, clock=clock, probe_timeout_s=cfg.local.probe_timeout_s
        )
    worker = LocalWorker(
        conn, cfg, clock, bodies, backends, controller.boot_id, notify=waiter.notify
    )
    registry = ClientRegistry(conn, cfg, clock)
    registry.on_revoke = controller.wake
    registry.sync()
    cloud = CloudState(conn, cfg, clock, has_adapter=lambda p: p in controller.adapters)
    rt = Runtime(
        conn=conn, cfg=cfg, clock=clock, controller=controller, worker=worker, bodies=bodies,
        registry=registry, waiter=waiter, cloud=cloud, started_at=clock.now(),
        version=__version__, config_path=config_path,
    )  # fmt: skip
    return Service(rt=rt, backends=backends, http_inference=http_backend,
                   safe_mode=opened.safe_mode)  # fmt: skip


async def serve(svc: Service, *, host: str | None = None, port: int | None = None) -> int:
    """uvicorn on `api.host:api.port` with the controller and worker loops in its lifespan.
    SIGHUP re-reads the clients; SIGTERM/SIGINT stop uvicorn, then clients are closed.
    Returns the process exit code: 0 on a clean stop, 1 when a background loop died."""
    import uvicorn

    rt = svc.rt
    app = create_app(rt, run_loops=True)
    config = uvicorn.Config(
        app, host=host or rt.cfg.api.host, port=rt.cfg.api.port if port is None else port,
        log_config=None, access_log=False, lifespan="on", timeout_graceful_shutdown=10,
    )  # fmt: skip
    server = uvicorn.Server(config)
    fatal: list[str] = []

    def on_fatal(name: str) -> None:
        fatal.append(name)
        server.should_exit = True

    rt.on_fatal = on_fatal
    if rt.config_path is not None:
        install_sighup(asyncio.get_running_loop(), rt.registry, rt.config_path)
    if threading.current_thread() is threading.main_thread():
        # uvicorn re-raises the captured SIGTERM/SIGINT after its shutdown, i.e. before our
        # cleanup; with these handlers in place the re-raise only repeats `should_exit`, so
        # clients are closed and the process exits with code 0 (systemd: clean stop)
        def graceful(sig: int, _frame: FrameType | None) -> None:
            if server.should_exit and sig == signal.SIGINT:
                server.force_exit = True  # a second Ctrl+C skips the graceful wait
            server.should_exit = True

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, graceful)
    try:
        await server.serve()
    finally:
        await svc.aclose()
        rt.conn.close()
    return 1 if fatal else 0


__all__ = [
    "Service", "build_adapters", "build_service", "make_speed_consumer", "mtp_key", "serve",
]  # fmt: skip
