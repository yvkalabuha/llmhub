"""T-369 W10: healthz/readyz, metrics, log masking, async routes, body limit, loop lag."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from api_fixtures import ADMIN_KEY, ApiEnv, api_config, chat_body
from llmhub.api_gateway import (
    routes_admin,
    routes_health,
    routes_leases,
    routes_openai,
    routes_tasks,
)
from llmhub.api_gateway.auth import require_admin
from llmhub.api_gateway.logging import SecretMaskFilter, configure_logging, mask
from llmhub.configuration.api_models import LocalBackendConfig
from llmhub.persistence import state as cstate

A = {"key": ADMIN_KEY}


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[ApiEnv]:
    e = ApiEnv(tmp_path)
    yield e
    await e.aclose()


async def test_healthz_no_key_and_independent_of_llm(env: ApiEnv) -> None:
    env.worker.is_available = False
    r = await env.get("/healthz", key=None)
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "ok" and j["version"] == "test" and j["halted"] is False
    assert j["uptime_s"] >= 0


async def test_readyz_needs_controller_heartbeat(env: ApiEnv) -> None:
    r = await env.get("/readyz", key=None)
    assert r.status_code == 503
    await env.pump(2)  # controller steps write the heartbeat
    assert (await env.get("/readyz", key=None)).status_code == 200
    env.clock.advance_to(env.clock.now() + 61)
    assert (await env.get("/readyz", key=None)).status_code == 503
    cstate.heartbeat(env.h.conn, env.clock)
    assert (await env.get("/readyz", key=None)).status_code == 200


def parse_prom(text: str) -> dict[str, list[tuple[dict[str, str], float]]]:
    out: dict[str, list[tuple[dict[str, str], float]]] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"([a-z_]+)(?:\{(.*)\})? (\S+)", line)
        assert m, line
        labels = dict(re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', m.group(2) or ""))
        out.setdefault(m.group(1), []).append((labels, float(m.group(3))))
    return out


async def test_metrics_admin_only_and_format(env: ApiEnv) -> None:
    assert (await env.get("/metrics", key=None)).status_code == 401
    assert (await env.get("/metrics")).status_code == 403
    await env.post("/v1/chat/completions", chat_body(model="localonly"), pump_s=2)
    env.worker.hold = True
    await env.post("/v1/tasks", chat_body("queued", model="localonly"))
    await env.post("/v1/tasks", chat_body("cloud", model="cloudonly"))
    r = await env.get("/metrics", **A)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    m = parse_prom(r.text)
    jobs = {(lab["status"], lab["route"]): v for lab, v in m["llmhub_jobs"]}
    assert jobs[("completed", "local")] == 1 and jobs[("queued", "local")] == 1
    assert jobs[("queued", "cloud")] == 1
    assert m["llmhub_local_queue_depth"][0][1] == 1
    assert any(lab == {"pool": "p1", "priority": "normal"} and v == 1
               for lab, v in m["llmhub_queue_depth"])  # fmt: skip
    assert m["llmhub_halted"][0][1] == 0 and m["llmhub_budget_stage"][0][1] == 0
    assert {lab["window"] for lab, _ in m["llmhub_budget_committed_usd"]} == {"month", "day"}
    assert m["llmhub_local_available"] == [({"backend": "cpu"}, 1.0)]
    assert {lab["kind"] for lab, _ in m["llmhub_local_ewma_tps"]} == {"pp", "tg"}
    assert "llmhub_loop_lag_s" in m and "llmhub_waiters" in m
    http = {(lab["path"], lab["code"]): v for lab, v in m["llmhub_http_requests_total"]}
    assert http[("/v1/chat/completions", "200")] == 1
    assert http[("/v1/tasks", "202")] == 2
    assert http[("/metrics", "401")] == 1 and http[("/metrics", "403")] == 1


async def test_metrics_halted_flag(env: ApiEnv) -> None:
    cstate.halt(env.h.conn, "admin", env.clock)
    m = parse_prom((await env.get("/metrics", **A)).text)
    assert m["llmhub_halted"][0][1] == 1


def test_mask() -> None:
    assert "abc123def456" not in mask("Authorization: Bearer abc123def456ghi")
    assert "Bearer ***" in mask("sent Bearer abc123def456ghi ok")
    assert mask("api_key=SECRETVALUE tail") == "api_key=*** tail"
    assert mask('{"api_key": "SECRETVALUE"}') == '{"api_key": "***"}'
    assert "lh_abcdefghijklmnop" not in mask("key lh_abcdefghijklmnop used")
    assert "lh_***" in mask("key lh_abcdefghijklmnop used")
    assert mask("nothing to hide") == "nothing to hide"


def test_filter_masks_formatted_record() -> None:
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "token=%s", ("tok123456789",), None)
    assert SecretMaskFilter().filter(rec) is True
    assert rec.getMessage() == "token=***"


def test_configure_logging_installs_filter() -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        configure_logging("INFO")
        configure_logging("INFO")  # idempotent
        for h in root.handlers:
            assert sum(isinstance(f, SecretMaskFilter) for f in h.filters) == 1
    finally:
        root.handlers[:] = before


async def test_request_bodies_never_logged(env: ApiEnv, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    needle = "UNIQUE-PROMPT-TEXT-98172"
    await env.post("/v1/chat/completions", chat_body(needle, model="localonly"), pump_s=2)
    await env.post("/v1/chat/completions", {"model": needle})  # validation error path
    await env.call("POST", "/v1/chat/completions", content=needle)  # bad JSON path
    assert needle not in caplog.text
    assert "lh_test_game_key" not in caplog.text


def walk(dependant: object) -> list[object]:
    calls = []
    for dep in getattr(dependant, "dependencies", []):
        calls.append(dep.call)
        calls.extend(walk(dep))
    return calls


def all_routes() -> list[APIRoute]:
    """FastAPI >= 0.160 wraps included routers lazily, so read the module routers directly."""
    out: list[APIRoute] = []
    for module in (routes_openai, routes_tasks, routes_leases, routes_admin, routes_health):
        out.extend(r for r in module.router.routes if isinstance(r, APIRoute))
    return out


async def test_all_routes_and_dependencies_are_coroutines(env: ApiEnv) -> None:
    routes = all_routes()
    assert len(routes) == 22
    seen_deps = 0
    for route in routes:
        assert inspect.iscoroutinefunction(route.endpoint), route.path
        for call in [*walk(route.dependant), *(d.dependency for d in route.dependencies)]:
            seen_deps += 1
            assert inspect.iscoroutinefunction(call), (route.path, call)
    for d in routes_admin.router.dependencies:
        assert d.dependency is require_admin
        assert inspect.iscoroutinefunction(d.dependency)
    assert seen_deps >= 15
    metrics = next(r for r in routes if r.path == "/metrics")
    assert [d.dependency for d in metrics.dependencies] == [require_admin]


async def test_route_set(env: ApiEnv) -> None:
    paths = env.app.openapi()["paths"]
    got = {(m.upper(), p) for p, ops in paths.items() for m in ops}
    for want in [
        ("POST", "/v1/chat/completions"), ("GET", "/v1/models"), ("POST", "/v1/tasks"),
        ("GET", "/v1/tasks/{task_id}"), ("GET", "/v1/tasks/{task_id}/result"),
        ("POST", "/v1/tasks/{task_id}/cancel"), ("POST", "/v1/leases"),
        ("POST", "/v1/leases/{lease_id}/renew"), ("POST", "/v1/leases/{lease_id}/close"),
        ("GET", "/admin/status"), ("GET", "/admin/budget"), ("GET", "/admin/clients"),
        ("POST", "/admin/reload_clients"), ("POST", "/admin/hold"), ("POST", "/admin/release"),
        ("POST", "/admin/kill"), ("POST", "/admin/resume"), ("POST", "/admin/reload_billing"),
        ("POST", "/admin/clear_weights_hold"), ("GET", "/healthz"), ("GET", "/readyz"),
        ("GET", "/metrics"),
    ]:  # fmt: skip
        assert want in got, want


async def test_body_limit_413(env: ApiEnv) -> None:
    limit = env.cfg.api.max_body_bytes
    big = json.dumps(chat_body("x" * (limit + 10)))
    r = await env.call("POST", "/v1/chat/completions", content=big)
    assert r.status_code == 413 and r.json()["error"]["code"] == "request_too_large"

    async def chunks() -> AsyncIterator[bytes]:  # no Content-Length: size is checked while reading
        for i in range(0, len(big), 100_000):
            yield big[i : i + 100_000].encode()

    r2 = await env.call("POST", "/v1/chat/completions", content=chunks())
    assert r2.status_code == 413
    assert env.jobs() == []
    ok = json.dumps(chat_body("y" * 1000, model="localonly"))
    assert (await env.call("POST", "/v1/tasks", content=ok)).status_code == 202


async def test_large_body_keeps_loop_responsive(tmp_path: Path) -> None:
    cfg = api_config(
        tmp_path,
        local_backends={
            "cpu": LocalBackendConfig(
                url="http://llama.invalid/v1", model="m", ctx_tokens=4_000_000, pp_tps=50, tg_tps=5
            )
        },
    )
    env = ApiEnv(tmp_path, cfg)
    try:
        await _large_body(env)
    finally:
        await env.aclose()


async def _large_body(env: ApiEnv) -> None:
    text = ("lorem ipsum dolor sit amet " * 150_000)[: env.cfg.api.max_body_bytes - 1000]
    body = json.dumps(chat_body(text, model="localonly"))
    assert len(body) > 3_900_000
    stop = asyncio.Event()
    monitor = asyncio.ensure_future(env.rt.lag.run(stop))
    await asyncio.sleep(0.2)
    env.rt.lag.take()
    r = await env.call("POST", "/v1/tasks", content=body)
    stop.set()
    await asyncio.gather(monitor, return_exceptions=True)
    assert r.status_code == 202, r.text
    assert env.rt.lag.take() < 0.1
    m = parse_prom((await env.get("/metrics", **A)).text)
    assert m["llmhub_loop_lag_s"][0][0] == {}
