from pathlib import Path

import pytest

from b_fixtures import file_store, label, make_provider, spec
from llmhub.provider_adapters.base import (
    Capability,
    DestroyResult,
    OfferQuery,
    ProviderError,
    RemoteStatus,
    ResourceKind,
    UnavailableReason,
)
from llmhub.provider_adapters.mock import Fault, MockKnobs, MockProvider
from llmhub.provider_adapters.mock_store import MockStore

H = 3600.0


async def _create(p: MockProvider, lab: str | None = None):
    return await p.create(spec(), lab or label())


async def test_lifecycle_and_status() -> None:
    p, clk = make_provider(MockKnobs(boot_s=60))
    res = await _create(p)
    assert res.status == RemoteStatus.PENDING and res.kind == ResourceKind.INSTANCE
    clk.advance_to(clk.now() + 61)
    got = await p.get(res.ref)
    assert got is not None and got.status == RemoteStatus.RUNNING
    assert [r.ref for r in await p.list_owned()] == [res.ref]
    assert await p.destroy(res.ref) == DestroyResult.ACCEPTED
    assert await p.get(res.ref) is None
    assert await p.destroy(res.ref) == DestroyResult.ALREADY_GONE
    assert await p.destroy("nope") == DestroyResult.ALREADY_GONE
    assert Capability.SPEND_RATE in p.capabilities


async def test_billing_per_second_and_balance() -> None:
    p, clk = make_provider(MockKnobs(price_usd_h=0.36, balance_usd=10.0))
    res = await _create(p)
    clk.advance_to(clk.now() + 1800)
    assert p.billed_total() == pytest.approx(0.18)
    bal = await p.balance()
    assert bal is not None and bal.usd == pytest.approx(9.82)
    await p.destroy(res.ref)
    clk.advance_to(clk.now() + H)
    assert p.billed_total() == pytest.approx(0.18)
    recs = await p.billing_since(0)
    assert sum(r.amount_usd for r in recs) == pytest.approx(0.18)
    assert p.billed_total(since=clk.now() - 100) == 0.0


async def test_destroy_delay_keeps_billing() -> None:
    p, clk = make_provider(MockKnobs(price_usd_h=3.6, destroy_delay_s=30))
    res = await _create(p)
    await p.destroy(res.ref)
    assert [r.ref for r in await p.list_owned()] == [res.ref]
    assert p.live_refs() == [res.ref]
    clk.advance_to(clk.now() + 29)
    assert await p.get(res.ref) is not None
    clk.advance_to(clk.now() + 2)
    assert await p.get(res.ref) is None and p.live_refs() == []
    assert p.billed_total() == pytest.approx(0.03)  # exactly 30 s at 3.6 $/h


async def test_zero_balance_deletes_everything() -> None:
    p, clk = make_provider(MockKnobs(price_usd_h=3.6, balance_usd=0.01))
    res = await _create(p)
    clk.advance_to(clk.now() + 60)
    assert await p.get(res.ref) is None
    with pytest.raises(ProviderError) as ei:
        await _create(p)
    assert ei.value.err.reason == UnavailableReason.INSUFFICIENT_BALANCE


async def test_list_owned_lag() -> None:
    p, clk = make_provider(MockKnobs(list_owned_lag_s=100))
    res = await _create(p)
    assert await p.list_owned() == []
    assert (await p.get(res.ref)) is not None
    assert p.live_refs() == [res.ref]
    clk.advance_to(clk.now() + 100)
    assert len(await p.list_owned()) == 1


async def test_shared_store_two_connections(tmp_path: Path) -> None:
    clk_p, clk = make_provider(store=file_store(tmp_path))
    other = MockProvider(MockStore(tmp_path / "cloud.db"), clk)
    res = await _create(clk_p)
    assert [r.ref for r in await other.list_owned()] == [res.ref]
    await other.destroy(res.ref)
    assert await clk_p.get(res.ref) is None
    # refs stay unique across connections
    a, b = await _create(clk_p), await _create(other)
    assert a.ref != b.ref


async def test_foreign_and_orphan() -> None:
    p, _ = make_provider()
    p.add_foreign("someone-else")
    ref = p.add_orphan(label())
    owned = await p.list_owned("llmhub-")
    assert [r.ref for r in owned] == [ref]
    assert {r.label for r in await p.list_owned("")} >= {"someone-else"}


async def test_interrupt_and_stopped_but_billing() -> None:
    p, clk = make_provider(MockKnobs(boot_s=0, price_usd_h=0.36))
    a, b = await _create(p), await _create(p)
    p.interrupt(a.ref)
    p.stopped_but_billing(b.ref)
    ga, gb = await p.get(a.ref), await p.get(b.ref)
    assert ga is not None and ga.status == RemoteStatus.TERMINATED
    assert gb is not None and gb.status == RemoteStatus.EXITED
    assert gb.spend_rate_usd_h is not None and gb.spend_rate_usd_h > 0
    clk.advance_to(clk.now() + H)
    assert p.billed_total() == pytest.approx(0.72)  # still billed until destroyed
    await p.destroy(b.ref)
    assert await p.get(b.ref) is None


# --- faults --------------------------------------------------------------------
async def test_create_timeout_created() -> None:
    p, _ = make_provider()
    p.inject("create", Fault.CREATE_TIMEOUT_CREATED)
    lab = label()
    with pytest.raises(ProviderError) as ei:
        await _create(p, lab)
    assert ei.value.err.outcome_unknown
    owned = await p.list_owned()
    assert [r.label for r in owned] == [lab] and len(p.live_refs()) == 1
    await _create(p)  # fault is spent


async def test_create_timeout_not_created() -> None:
    p, _ = make_provider()
    p.inject("create", Fault.CREATE_TIMEOUT_NOT_CREATED)
    with pytest.raises(ProviderError) as ei:
        await _create(p)
    assert ei.value.err.outcome_unknown and p.live_refs() == []


async def test_create_timeout_created_late() -> None:
    p, clk = make_provider(MockKnobs(late_visible_s=300))
    p.inject("create", Fault.CREATE_TIMEOUT_CREATED_LATE)
    lab = label()
    with pytest.raises(ProviderError):
        await _create(p, lab)
    assert await p.list_owned() == [] and len(p.live_refs()) == 1  # invisible but billed
    await _create(p, lab)  # the retry creates a second one
    clk.advance_to(clk.now() + 301)
    assert len([r for r in await p.list_owned() if r.label == lab]) == 2


async def test_create_5xx_and_simple_errors() -> None:
    p, _ = make_provider()
    cases = [
        (Fault.CREATE_5XX, UnavailableReason.PROVIDER_API_ERROR, True),
        (Fault.INSUFFICIENT_BALANCE, UnavailableReason.INSUFFICIENT_BALANCE, False),
        (Fault.QUOTA, UnavailableReason.QUOTA_EXCEEDED, False),
        (Fault.GPU_UNAVAILABLE, UnavailableReason.GPU_UNAVAILABLE, False),
    ]
    for fault, reason, retryable in cases:
        p.inject("create", fault)
        with pytest.raises(ProviderError) as ei:
            await _create(p)
        assert ei.value.err.reason == reason and ei.value.err.retryable == retryable
        assert not ei.value.err.outcome_unknown
    assert p.live_refs() == []


async def test_create_ghost_ref() -> None:
    p, _ = make_provider()
    p.inject("create", Fault.CREATE_GHOST_REF)
    lab = label()
    res = await _create(p, lab)
    assert res.ref.startswith("ghost-") and res.label == lab
    assert await p.get(res.ref) is None
    owned = await p.list_owned()
    assert len(owned) == 1 and owned[0].label == lab and owned[0].ref != res.ref


async def test_create_hang_returns_after_hang_s() -> None:
    import asyncio

    p, clk = make_provider(MockKnobs(hang_s=3600))
    p.inject("create", Fault.CREATE_HANG)
    task = asyncio.ensure_future(_create(p))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not task.done() and len(p.live_refs()) == 1  # resource exists, call still hangs
    clk.advance_to(clk.now() + 3600)
    res = await task
    assert res.ref in p.live_refs()


async def test_rate_limit_and_5xx_on_any_op() -> None:
    p, _ = make_provider()
    r = await _create(p)
    p.inject("get", Fault.RATE_LIMIT_429, times=2)
    for _ in range(2):
        with pytest.raises(ProviderError) as ei:
            await p.get(r.ref)
        assert ei.value.err.http_status == 429 and ei.value.err.retry_after_s
    assert await p.get(r.ref) is not None
    p.inject("any", Fault.API_5XX, times=2)
    with pytest.raises(ProviderError):
        await p.list_owned()
    with pytest.raises(ProviderError):
        await p.balance()
    assert await p.balance() is not None


async def test_fault_op_validation() -> None:
    p, _ = make_provider()
    with pytest.raises(ValueError):
        p.inject("get", Fault.DESTROY_FAIL)


async def test_gpu_unavailable_on_list_offers() -> None:
    p, _ = make_provider()
    q = OfferQuery("g", 24, 1.0, frozenset())
    p.inject("list_offers", Fault.GPU_UNAVAILABLE)
    assert await p.list_offers(q) == []
    assert len(await p.list_offers(q)) == 3


async def test_destroy_fail_then_ok() -> None:
    p, _ = make_provider()
    r = await _create(p)
    p.inject("destroy", Fault.DESTROY_FAIL, times=2)
    for _ in range(2):
        with pytest.raises(ProviderError):
            await p.destroy(r.ref)
    assert p.live_refs() == [r.ref]
    assert await p.destroy(r.ref) == DestroyResult.ACCEPTED and p.live_refs() == []


async def test_destroy_never_keeps_billing() -> None:
    p, clk = make_provider(MockKnobs(price_usd_h=3.6))
    r = await _create(p)
    p.inject("destroy", Fault.DESTROY_NEVER)
    assert await p.destroy(r.ref) == DestroyResult.ACCEPTED
    assert await p.destroy(r.ref) == DestroyResult.ACCEPTED
    clk.advance_to(clk.now() + 1000)
    assert p.live_refs() == [r.ref] and await p.get(r.ref) is not None


async def test_destroy_429() -> None:
    p, _ = make_provider()
    r = await _create(p)
    p.inject("destroy", Fault.DESTROY_429)
    with pytest.raises(ProviderError) as ei:
        await p.destroy(r.ref)
    assert ei.value.err.http_status == 429 and ei.value.err.retry_after_s == 30.0
    assert ei.value.err.retryable


async def test_destroy_partial_leaves_billed_volume() -> None:
    p, clk = make_provider(MockKnobs(volume_usd_h=0.36))
    r = await _create(p)
    p.inject("destroy", Fault.DESTROY_PARTIAL)
    assert await p.destroy(r.ref) == DestroyResult.ACCEPTED
    owned = await p.list_owned()
    assert len(owned) == 1 and owned[0].kind == ResourceKind.VOLUME and owned[0].label == r.label
    before = p.billed_total()
    clk.advance_to(clk.now() + H)
    assert p.billed_total() - before == pytest.approx(0.36)
    assert await p.destroy(owned[0].ref) == DestroyResult.ACCEPTED
    assert await p.list_owned() == [] and p.live_refs() == []


async def test_list_stale_shows_old_snapshot() -> None:
    p, clk = make_provider(MockKnobs(stale_s=300))
    old = await _create(p)
    clk.advance_to(clk.now() + 1000)
    new = await _create(p)
    p.inject("list_owned", Fault.LIST_STALE)
    assert [r.ref for r in await p.list_owned()] == [old.ref]  # new one not yet in the snapshot
    await p.destroy(old.ref)
    p.inject("list_owned", Fault.LIST_STALE)
    assert {r.ref for r in await p.list_owned()} == {old.ref}  # destroyed one still shown
    assert {r.ref for r in await p.list_owned()} == {new.ref}


def test_normalize_error_hides_message_text() -> None:
    p, _ = make_provider()
    err = p.normalize_error(RuntimeError("key=sk-SECRET"))
    assert "SECRET" not in err.message
    assert p.normalize_error(TimeoutError()).outcome_unknown
