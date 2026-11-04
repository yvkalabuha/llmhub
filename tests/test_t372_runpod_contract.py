"""T-372 A5: RunpodAdapter on the fake RunPod passes the adapter contract C1..C11."""

from __future__ import annotations

import pytest

from llmhub.provider_adapters.contract import run_contract
from runpod_fake import RunpodHarness

SECRET = b"test-secret-0123456789abcdef-test-secret"


@pytest.mark.parametrize("lagging", [False, True])
async def test_runpod_passes_contract(lagging: bool) -> None:
    h = RunpodHarness(lagging=lagging)
    await run_contract(h, SECRET)
    await h.settle()
    assert [p.name for p in h.fake.live_pods()] == ["someone-elses-instance"]


async def test_secret_env_never_echoed_by_fake_answers() -> None:
    """The fake returns `env` like RunPod; C11 proves the adapter drops it."""
    h = RunpodHarness()
    await run_contract(h, SECRET)
    assert any("env" in b for b in h.fake.created_bodies)
