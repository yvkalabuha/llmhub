import asyncio

import pytest

from b_fixtures import label, make_provider, spec
from llmhub.cloud_manager.inference import InstanceLost, JobRunSpec, NetworkLost
from llmhub.provider_adapters.mock import MockKnobs
from llmhub.provider_adapters.mock_inference import MockInference, MockInferenceKnobs

JOB = JobRunSpec("j1", "m1", tokens_in=1200, max_tokens=780, body=b"x")  # 1 s + 10 s


async def _ready(knobs: MockInferenceKnobs | None = None):
    p, clk = make_provider(MockKnobs(boot_s=10))
    inf = MockInference(p, clk, knobs)
    res = await p.create(spec(), label())
    assert res.endpoint is not None
    return p, clk, inf, res


async def test_ping_and_health_follow_boot_and_load() -> None:
    _p, clk, inf, res = await _ready(MockInferenceKnobs(load_s=6))
    ep = res.endpoint or ""
    assert not await inf.ping(ep)
    clk.advance_to(clk.now() + 10)
    assert await inf.ping(ep) and not await inf.health(ep)
    clk.advance_to(clk.now() + 6)
    assert await inf.health(ep) and await inf.test_request(ep)


async def test_test_fail_counts_down() -> None:
    _p, clk, inf, res = await _ready(MockInferenceKnobs(test_fail=2, load_s=0))
    clk.advance_to(clk.now() + 10)
    ep = res.endpoint or ""
    assert [await inf.test_request(ep) for _ in range(3)] == [False, False, True]


async def test_hang_load_and_health_never() -> None:
    for knobs in (MockInferenceKnobs(hang_load=True), MockInferenceKnobs(health_never=True)):
        _p, clk, inf, res = await _ready(knobs)
        clk.advance_to(clk.now() + 3600)
        assert await inf.ping(res.endpoint or "") and not await inf.health(res.endpoint or "")


async def test_run_takes_model_time() -> None:
    _p, clk, inf, res = await _ready()
    clk.advance_to(clk.now() + 10)
    start = clk.now()
    task = asyncio.ensure_future(inf.run(res.endpoint or "", JOB))
    await asyncio.sleep(0)
    assert clk.next_waiter() == pytest.approx(start + 1 + 10)
    clk.advance_to(start + 11)
    out = await task
    assert (out.tokens_in, out.tokens_out, out.finish_reason) == (1200, 780, "stop")


async def test_run_instance_lost_on_interrupt() -> None:
    p, clk, inf, res = await _ready()
    clk.advance_to(clk.now() + 10)
    task = asyncio.ensure_future(inf.run(res.endpoint or "", JOB))
    await asyncio.sleep(0)
    p.interrupt(res.ref)
    clk.advance_to(clk.now() + 11)
    with pytest.raises(InstanceLost):
        await task


async def test_run_instance_lost_on_destroy() -> None:
    p, clk, inf, res = await _ready()
    clk.advance_to(clk.now() + 10)
    task = asyncio.ensure_future(inf.run(res.endpoint or "", JOB))
    await asyncio.sleep(0)
    await p.destroy(res.ref)
    clk.advance_to(clk.now() + 11)
    with pytest.raises(InstanceLost):
        await task


async def test_run_network_lost() -> None:
    _p, clk, inf, res = await _ready()
    clk.advance_to(clk.now() + 10)
    task = asyncio.ensure_future(inf.run(res.endpoint or "", JOB))
    await asyncio.sleep(0)
    inf.lose_network(res.ref, until=clk.now() + 500)
    clk.advance_to(clk.now() + 11)
    with pytest.raises(NetworkLost):
        await task
    assert not await inf.ping(res.endpoint or "")
    clk.advance_to(clk.now() + 500)
    assert await inf.ping(res.endpoint or "")
