import asyncio
from collections.abc import Awaitable
from pathlib import Path

import pytest

from b_fixtures import label, make_provider, spec
from llmhub.clock import FakeClock
from llmhub.configuration.models import RetryConfig
from llmhub.persistence.db import connect, migrate, tx
from llmhub.provider_adapters.base import (
    DestroyResult,
    OfferQuery,
    ProviderError,
    UnavailableReason,
)
from llmhub.provider_adapters.guarded import CircuitBreaker, GuardedAdapter
from llmhub.provider_adapters.mock import Fault, MockKnobs, MockProvider

Q = OfferQuery("g", 24, 1.0, frozenset())


async def drive[T](clk: FakeClock, aw: Awaitable[T], limit: int = 200) -> T:
    """Run `aw`, moving the FakeClock to the next waiter whenever the task is blocked."""
    task = asyncio.ensure_future(aw)
    for _ in range(limit):
        for _ in range(5):
            await asyncio.sleep(0)
        if task.done():
            break
        nxt = clk.next_waiter()
        assert nxt is not None, "task blocked with no timer"
        clk.advance_to(nxt)
    return await task


def env(tmp_path: Path, knobs: MockKnobs | None = None, actor: str = "controller", **retry):
    p, clk = make_provider(knobs)
    conn = connect(tmp_path / "state.db")
    migrate(conn)
    cfg = RetryConfig(**retry)
    br = CircuitBreaker(conn, "mock", clk, cfg)
    g = GuardedAdapter(p, clk, cfg, br, actor=actor)  # type: ignore[arg-type]
    return p, clk, conn, br, g


async def test_get_retried_three_times_with_backoff(tmp_path: Path) -> None:
    p, clk, _, _, g = env(tmp_path)
    r = await p.create(spec(), label())
    p.inject("get", Fault.API_5XX, times=3)
    t0 = clk.now()
    res = await drive(clk, g.get(r.ref))
    assert res is not None
    assert clk.now() - t0 == pytest.approx(1 + 2 + 4)


async def test_get_gives_up_after_retries(tmp_path: Path) -> None:
    p, clk, _, _, g = env(tmp_path)
    r = await p.create(spec(), label())
    p.inject("get", Fault.API_5XX, times=10)
    with pytest.raises(ProviderError):
        await drive(clk, g.get(r.ref))


async def test_retry_after_is_honoured(tmp_path: Path) -> None:
    p, clk, _, _, g = env(tmp_path)
    r = await p.create(spec(), label())
    p.inject("get", Fault.RATE_LIMIT_429)
    t0 = clk.now()
    await drive(clk, g.get(r.ref))
    assert clk.now() - t0 == pytest.approx(5.0)


async def test_non_retryable_not_repeated(tmp_path: Path) -> None:
    p, clk, _, _, g = env(tmp_path)
    p.inject("create", Fault.INSUFFICIENT_BALANCE, times=5)
    with pytest.raises(ProviderError) as ei:
        await drive(clk, g.create(spec(), label()))
    assert ei.value.err.reason == UnavailableReason.INSUFFICIENT_BALANCE
    assert p.create_calls == 1


async def test_create_is_not_retried(tmp_path: Path) -> None:
    p, clk, _, _, g = env(tmp_path)
    p.inject("create", Fault.CREATE_5XX, times=5)
    with pytest.raises(ProviderError):
        await drive(clk, g.create(spec(), label()))
    assert p.create_calls == 1


async def test_destroy_is_not_retried(tmp_path: Path) -> None:
    p, clk, _, _, g = env(tmp_path)
    r = await p.create(spec(), label())
    p.inject("destroy", Fault.DESTROY_FAIL, times=1)
    with pytest.raises(ProviderError):
        await drive(clk, g.destroy(r.ref))
    assert p.live_refs() == [r.ref]  # no hidden retry
    assert await drive(clk, g.destroy(r.ref)) == DestroyResult.ACCEPTED


async def test_create_hang_times_out_after_20s_as_unknown(tmp_path: Path) -> None:
    p, clk, _, _, g = env(tmp_path)
    p.inject("create", Fault.CREATE_HANG)
    t0 = clk.now()
    with pytest.raises(ProviderError) as ei:
        await drive(clk, g.create(spec(), label()))
    assert clk.now() - t0 == pytest.approx(20.0)
    assert ei.value.err.outcome_unknown
    assert len(p.live_refs()) == 1  # the provider did create it: found later by label


async def test_get_timeout_is_not_unknown_and_is_retried(tmp_path: Path) -> None:
    p, clk, _, _, g = env(tmp_path)
    r = await p.create(spec(), label())
    real = p.get
    calls = {"n": 0}

    async def slow_get(ref: str):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            await clk.sleep_until(clk.now() + 1000)
        return await real(ref)

    p.get = slow_get  # type: ignore[method-assign]
    assert await drive(clk, g.get(r.ref)) is not None
    assert calls["n"] == 2


async def test_custom_call_timeout(tmp_path: Path) -> None:
    p, clk, _, _, g = env(tmp_path, call_timeout_s=5)
    p.inject("create", Fault.CREATE_HANG)
    t0 = clk.now()
    with pytest.raises(ProviderError):
        await drive(clk, g.create(spec(), label()))
    assert clk.now() - t0 == pytest.approx(5.0)


async def test_call_inside_tx_is_rejected(tmp_path: Path) -> None:
    _p, _clk, conn, _, g = env(tmp_path)
    with tx(conn), pytest.raises(AssertionError):
        await g.list_owned()
    with tx(conn), pytest.raises(AssertionError):
        await g.destroy("x")


async def _fail_creates(p: MockProvider, g: GuardedAdapter, clk: FakeClock, n: int) -> None:
    p.inject("create", Fault.CREATE_5XX, times=n)
    for _ in range(n):
        with pytest.raises(ProviderError):
            await drive(clk, g.create(spec(), label()))


async def test_breaker_closed_open_half_open_closed(tmp_path: Path) -> None:
    p, clk, _, br, g = env(tmp_path)
    assert br.state == "closed"
    await _fail_creates(p, g, clk, 3)
    assert br.state == "open"
    calls = p.create_calls
    with pytest.raises(ProviderError) as ei:
        await drive(clk, g.create(spec(), label()))
    assert p.create_calls == calls and "circuit open" in ei.value.err.message  # not even tried
    assert ei.value.err.retry_after_s == pytest.approx(900)
    with pytest.raises(ProviderError):
        await drive(clk, g.list_offers(Q))
    clk.advance_to(clk.now() + 900)
    res = await drive(clk, g.create(spec(), label()))  # the single half-open probe succeeds
    assert res.ref and br.state == "closed"


async def test_half_open_allows_one_probe_and_failure_reopens(tmp_path: Path) -> None:
    p, clk, _, br, g = env(tmp_path)
    await _fail_creates(p, g, clk, 3)
    clk.advance_to(clk.now() + 900)
    assert br.allow() is True and br.state == "half_open"
    assert br.allow() is False  # second concurrent probe is refused
    br.record_error()
    assert br.state == "open" and br.retry_in() == pytest.approx(900)


async def test_open_breaker_lets_destroy_get_list_pass(tmp_path: Path) -> None:
    p, clk, _, br, g = env(tmp_path)
    r = await p.create(spec(), label())
    await _fail_creates(p, g, clk, 3)
    assert br.state == "open"
    assert await drive(clk, g.get(r.ref)) is not None
    assert len(await drive(clk, g.list_owned())) == 1
    assert await drive(clk, g.balance()) is not None
    assert await drive(clk, g.billing_since(0)) is not None
    assert await drive(clk, g.destroy(r.ref)) == DestroyResult.ACCEPTED


async def test_errors_outside_window_do_not_open(tmp_path: Path) -> None:
    p, clk, _, br, g = env(tmp_path)
    await _fail_creates(p, g, clk, 2)
    clk.advance_to(clk.now() + 601)
    await _fail_creates(p, g, clk, 2)
    assert br.state == "closed"


async def test_business_errors_do_not_open(tmp_path: Path) -> None:
    p, clk, _, br, g = env(tmp_path)
    p.inject("create", Fault.GPU_UNAVAILABLE, times=5)
    for _ in range(5):
        with pytest.raises(ProviderError):
            await drive(clk, g.create(spec(), label()))
    assert br.state == "closed"


async def test_destroy_429_does_not_open_breaker(tmp_path: Path) -> None:
    p, clk, _, br, g = env(tmp_path)
    r = await p.create(spec(), label())
    p.inject("destroy", Fault.DESTROY_429, times=5)
    for _ in range(5):
        with pytest.raises(ProviderError):
            await drive(clk, g.destroy(r.ref))
    assert br.state == "closed"


async def test_get_errors_open_breaker_for_create_only(tmp_path: Path) -> None:
    p, clk, _, br, g = env(tmp_path)
    r = await p.create(spec(), label())
    p.inject("get", Fault.API_5XX, times=3)
    assert await drive(clk, g.get(r.ref)) is not None  # 3 failures, then success
    assert br.state == "open"
    with pytest.raises(ProviderError):
        await drive(clk, g.create(spec(), label()))


async def test_reaper_errors_do_not_count_and_are_not_gated(tmp_path: Path) -> None:
    p, clk, conn, br, g = env(tmp_path, actor="reaper")
    p.inject("create", Fault.CREATE_5XX, times=5)
    for _ in range(5):
        with pytest.raises(ProviderError):
            await drive(clk, g.create(spec(), label()))
    assert br.state == "closed"
    # a controller-side breaker that is already open does not stop the reaper's calls either
    conn.execute("UPDATE provider_accounts SET circuit_state='open', circuit_opened_at=?",
                 (clk.now(),))  # fmt: skip
    res = await drive(clk, g.create(spec(), label()))
    assert res.ref


async def test_breaker_state_survives_restart(tmp_path: Path) -> None:
    p, clk, conn, _br, g = env(tmp_path)
    await _fail_creates(p, g, clk, 3)
    br2 = CircuitBreaker(conn, "mock", clk, RetryConfig())
    assert br2.state == "open" and br2.allow() is False


def test_guard_exposes_inner_identity(tmp_path: Path) -> None:
    p, _, _, _, g = env(tmp_path)
    assert g.name == p.name and g.capabilities == p.capabilities
