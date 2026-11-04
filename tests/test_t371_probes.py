"""T-371 D2 (F8, Q3): background probes are GET /slots|/health|/v1/models, >= 10 s apart,
and nothing else (no completion, no POST) reaches llama-server in the background."""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path

import pytest

from h_fixtures import LocalEnv, lspec

INTERVAL = 30.0
N = 10
PATHS = {"slots": "/slots", "health": "/health", "models": "/v1/models"}


async def _drive(env: LocalEnv, seconds: float, step: float = 1.0) -> None:
    end = env.clock.now() + seconds
    while env.clock.now() < end:
        env.clock.advance_to(env.clock.now() + step)
        for _ in range(20):
            await asyncio.sleep(0)


async def _run(env: LocalEnv, seconds: float) -> None:
    stop = asyncio.Event()
    runner = asyncio.ensure_future(env.worker.run_forever(stop))
    try:
        await _drive(env, seconds)
    finally:
        stop.set()
        await asyncio.wait_for(runner, 5)


@pytest.mark.parametrize("mode", ["slots", "health", "models"])
async def test_idle_background_traffic_is_get_probes_only(tmp_path: Path, mode: str) -> None:
    env = LocalEnv(tmp_path, probe_interval_s=INTERVAL)
    for b in env.cfg.local_backends.values():
        b.probe = mode  # type: ignore[assignment]
    env.worker = env.make_worker()
    await _run(env, N * INTERVAL)
    for name, fake in env.fakes.items():
        seen = {(r.method, r.path) for r in fake.requests}
        assert seen == {("GET", PATHS[mode])}, (name, seen)
        assert 1 <= len(fake.requests) <= N + 1, (name, len(fake.requests))


async def test_running_job_adds_no_extra_probes(tmp_path: Path) -> None:
    env = LocalEnv(tmp_path, probe_interval_s=INTERVAL)
    env.fakes["cpu"].delays.append(5 * INTERVAL)  # one long completion
    env.submit(lspec(1))
    await _run(env, N * INTERVAL)
    fake = env.fakes["cpu"]
    posts = [r for r in fake.requests if r.method != "GET"]
    assert [(r.method, r.path) for r in posts] == [("POST", "/v1/chat/completions")]
    probes = [r for r in fake.requests if r.method == "GET"]
    assert {r.path for r in probes} == {"/slots"}
    assert len(probes) <= N + 1
    other = env.fakes["cpu2"]
    assert all(r.method == "GET" for r in other.requests) and len(other.requests) <= N + 1


async def test_probe_timestamps_respect_interval(tmp_path: Path) -> None:
    env = LocalEnv(tmp_path, probe_interval_s=10)
    times: list[float] = []
    orig = env.worker.probe_all

    async def spy() -> None:
        times.append(env.clock.now())
        await orig()

    env.worker.probe_all = spy  # type: ignore[method-assign]
    await _run(env, 100)
    gaps = [b - a for a, b in itertools.pairwise(times)]
    assert gaps and min(gaps) >= 10
