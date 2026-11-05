"""T-372 (2026-11-05): a pool gpu class maps to an ordered list of RunPod GPU types.

Customer decision: no RTX 3090 with CUDA >= 12.8 on Community, so the 27B pool also takes the
RTX 3090 Ti (0.27) and RTX 4090 (0.34), pool cap 0.35. Offers: one per type with a Community
price within the cap, cheapest first. Create: types by price ascending (within the cap), for
each the CUDA versions highest first; the next attempt only after a definite `no_capacity`.
Fake RunPod only, no live calls."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from llmhub.clock import FakeClock
from llmhub.configuration.loader import load_config
from llmhub.provider_adapters.base import (
    CreatedResourceRejected,
    InstanceSpec,
    OfferQuery,
    ProviderError,
    RemoteResource,
    UnavailableReason,
)
from llmhub.provider_adapters.labels import make_label, new_op_id
from runpod_fake import NO_INSTANCES_BODY, FakeRunpod, make_adapter, make_spec, runpod_cfg

SECRET = b"test-secret-0123456789abcdef-test-secret"
G3090 = "NVIDIA GeForce RTX 3090"
G3090TI = "NVIDIA GeForce RTX 3090 Ti"
G4090 = "NVIDIA GeForce RTX 4090"
PRICES: dict[str, float | None] = {G3090: 0.22, G3090TI: 0.27, G4090: 0.34}
LIST = {"RTX24G": [G3090, G3090TI, G4090]}
ROOT = Path(__file__).resolve().parents[1]


def _label() -> str:
    return make_label(new_op_id(), SECRET)


def _query(cap: float = 0.35, gpu: str = "RTX24G") -> OfferQuery:
    return OfferQuery(gpu=gpu, min_vram_gb=24, max_price_usd_h=cap, exclude_hosts=frozenset())


def _posts(fake: FakeRunpod) -> list[tuple[str, str]]:
    return [(b["gpuTypeIds"][0], b["allowedCudaVersions"][0]) for b in fake.created_bodies]


def _gets(fake: FakeRunpod) -> int:
    return sum(1 for r in fake.requests if r.method == "GET" and r.url.path == "/v1/pods")


def _fake(**kw: Any) -> FakeRunpod:
    return FakeRunpod(FakeClock(), gpus=dict(PRICES), **kw)


def _spec(**over: Any) -> InstanceSpec:
    return make_spec(**{"offer_id": G3090, "max_price_usd_h": 0.35, **over})


# ------------------------------------------------------------------ config
def test_config_str_value_is_backward_compatible() -> None:
    rp = runpod_cfg()
    assert rp.gpu_type_ids == {"RTX3090": G3090}
    assert rp.gpu_ids("RTX3090") == [G3090] and rp.gpu_ids("H100") == []
    assert rp.all_gpu_ids() == [G3090]


def test_config_list_value_keeps_order() -> None:
    rp = runpod_cfg(gpu_type_ids={**LIST, "RTX3090": G3090})
    assert rp.gpu_ids("RTX24G") == [G3090, G3090TI, G4090]
    assert rp.class_of(G4090) == "RTX24G" and rp.class_of("NVIDIA A40") is None
    assert set(rp.all_gpu_ids()) == {G3090, G3090TI, G4090}


@pytest.mark.parametrize("bad", [[], [""], [G3090, G3090], ""])
def test_config_rejects_empty_or_duplicate_ids(bad: Any) -> None:
    with pytest.raises(ValidationError):
        runpod_cfg(gpu_type_ids={"RTX24G": bad})


def test_example_config_uses_the_widened_class() -> None:
    cfg = load_config(ROOT / "deploy" / "config.t372.example.yaml", {})
    rp = cfg.providers.runpod
    assert rp is not None and rp.gpu_ids("RTX24G") == [G3090, G3090TI, G4090]
    assert rp.price_ceiling_usd_h == 0.50
    (pool,) = cfg.pools.values()
    assert pool.gpu == "RTX24G" and pool.max_price_usd_h == 0.35


# ------------------------------------------------------------------ offers
async def test_offers_one_per_type_within_cap_cheapest_first() -> None:
    fake = _fake()
    a = make_adapter(fake, gpu_type_ids={"RTX24G": [G4090, G3090TI, G3090]})  # unsorted list
    offers = await a.list_offers(_query(0.35))
    assert [(o.offer_id, o.price_usd_h) for o in offers] == [(G3090, 0.22), (G3090TI, 0.27),
                                                             (G4090, 0.34)]  # fmt: skip
    assert {o.gpu for o in offers} == {"RTX24G"}


async def test_offers_drop_types_above_the_pool_cap() -> None:
    offers = await make_adapter(_fake(), gpu_type_ids=LIST).list_offers(_query(0.30))
    assert [o.offer_id for o in offers] == [G3090, G3090TI]


async def test_offers_skip_types_without_community() -> None:
    fake = FakeRunpod(FakeClock(), gpus={G3090: None, G3090TI: 0.27, G4090: 0.34})
    offers = await make_adapter(fake, gpu_type_ids=LIST).list_offers(_query())
    assert [o.offer_id for o in offers] == [G3090TI, G4090]


async def test_offers_unknown_class_is_empty() -> None:
    assert await make_adapter(_fake(), gpu_type_ids=LIST).list_offers(_query(gpu="H100")) == []


# ------------------------------------------------------------------ create
async def test_create_takes_the_cheapest_type_first() -> None:
    fake = _fake()
    a = make_adapter(fake, gpu_type_ids=LIST)
    res = await a.create(_spec(offer_id=G4090), _label())  # cheapest first, whatever the offer
    assert _posts(fake) == [(G3090, "13.0")]
    assert a.last_create_attempts == [(G3090, "13.0", "created")]
    assert res.offer_id == G3090 and res.price_usd_h == pytest.approx(0.225)


async def test_create_walks_types_and_versions_on_no_capacity_only() -> None:
    fake = _fake(gpu_capacity={G4090})
    a = make_adapter(fake, gpu_type_ids=LIST)
    res = await a.create(_spec(), _label())
    v = ["13.0", "12.9", "12.8"]
    assert _posts(fake) == [(G3090, x) for x in v] + [(G3090TI, x) for x in v] + [
        (G4090, "13.0")]
    assert a.last_create_attempts[-1] == (G4090, "13.0", "created")
    assert [r for *_, r in a.last_create_attempts[:-1]] == ["no_capacity"] * 6
    assert _gets(fake) == 1  # max_live_pods checked once
    assert res.offer_id == G4090 and res.price_usd_h == pytest.approx(0.345)
    assert [p.id for p in fake.live_pods()] == [res.ref]


async def test_types_above_the_cap_are_never_posted() -> None:
    fake = _fake(gpu_capacity={G4090})
    a = make_adapter(fake, gpu_type_ids=LIST)
    with pytest.raises(ProviderError) as ei:
        await a.create(_spec(max_price_usd_h=0.30), _label())
    assert ei.value.err.reason == UnavailableReason.GPU_UNAVAILABLE
    assert not ei.value.err.outcome_unknown and ei.value.err.retryable
    assert {g for g, _ in _posts(fake)} == {G3090, G3090TI} and fake.posts() == 6
    assert G3090 in ei.value.err.message and G4090 not in ei.value.err.message


async def test_adapter_ceiling_caps_the_list_too() -> None:
    fake = _fake(gpu_capacity={G4090})
    a = make_adapter(fake, gpu_type_ids=LIST, price_ceiling_usd_h=0.30)
    with pytest.raises(ProviderError):
        await a.create(_spec(max_price_usd_h=0.35), _label())
    assert G4090 not in {g for g, _ in _posts(fake)}


async def test_unknown_outcome_stops_before_the_next_type() -> None:
    fake = _fake(gpu_capacity={G3090TI, G4090})
    for _ in range(3):
        fake.inject("POST", "/pods", "status", status=500, body=NO_INSTANCES_BODY)
    fake.inject("POST", "/pods", "timeout_after")
    a = make_adapter(fake, gpu_type_ids=LIST)
    with pytest.raises(ProviderError) as ei:
        await a.create(_spec(), _label())
    assert ei.value.err.outcome_unknown and fake.posts() == 4
    assert a.last_create_attempts[-1] == (G3090TI, "13.0", "unknown")


async def test_other_refusal_stops_before_the_next_type() -> None:
    fake = _fake()
    fake.inject("POST", "/pods", "status", status=400, body='{"error":"bad request"}')
    a = make_adapter(fake, gpu_type_ids=LIST)
    with pytest.raises(ProviderError) as ei:
        await a.create(_spec(), _label())
    assert not ei.value.err.outcome_unknown and fake.posts() == 1
    assert a.last_create_attempts == [(G3090, "13.0", "deployment_error")]


async def test_every_type_above_the_cap_is_refused_before_post() -> None:
    fake = FakeRunpod(FakeClock(), gpus={G3090: 0.40, G3090TI: 0.41, G4090: 0.45})
    a = make_adapter(fake, gpu_type_ids=LIST)
    with pytest.raises(ProviderError) as ei:
        await a.create(_spec(), _label())
    assert ei.value.err.reason == UnavailableReason.DEPLOYMENT_ERROR
    assert "above ceiling" in ei.value.err.message and fake.posts() == 0


async def test_no_community_type_at_all_is_gpu_unavailable() -> None:
    fake = FakeRunpod(FakeClock(), gpus={G3090: None, G3090TI: None, G4090: None})
    with pytest.raises(ProviderError) as ei:
        await make_adapter(fake, gpu_type_ids=LIST).create(_spec(), _label())
    assert ei.value.err.reason == UnavailableReason.GPU_UNAVAILABLE and fake.posts() == 0


async def test_gpu_outside_every_class_is_refused() -> None:
    fake = _fake()
    with pytest.raises(ProviderError) as ei:
        await make_adapter(fake, gpu_type_ids=LIST).create(_spec(offer_id="NVIDIA A40"),
                                                           _label())  # fmt: skip
    assert "gpu type not allowed" in ei.value.err.message and fake.posts() == 0


async def test_created_cost_above_cap_is_rejected() -> None:
    fake = _fake(cost_override=0.36)
    a = make_adapter(fake, gpu_type_ids=LIST)
    with pytest.raises(CreatedResourceRejected) as ei:
        await a.create(_spec(), _label())
    assert "price" in ei.value.what and ei.value.resource.ref in fake.pods


async def test_str_config_still_creates_one_type() -> None:
    fake = FakeRunpod(FakeClock(), cuda_capacity={"12.8"})
    a = make_adapter(fake)
    await a.create(make_spec(), _label())
    assert _posts(fake) == [(G3090, "13.0"), (G3090, "12.9"), (G3090, "12.8")]
    assert a.last_create_attempts == [(G3090, "13.0", "no_capacity"),
                                      (G3090, "12.9", "no_capacity"),
                                      (G3090, "12.8", "created")]  # fmt: skip


# ------------------------------------------------------------------ lifecycle-probe
async def test_probe_reports_gpu_cuda_attempts(tmp_path: Path) -> None:
    from llmhub.persistence.db import connect, migrate
    from llmhub.provider_adapters.runpod import RunpodAdapter
    from llmhub.tools.t372_ops import lifecycle_probe
    from test_review_t372_fn_runpod_real import SMALL, _AutoClock, _cfg

    clock = _AutoClock()
    fake = FakeRunpod(clock, gpus=dict(PRICES), gpu_capacity={G3090TI}, cuda_capacity={"12.9"})
    adapter = RunpodAdapter(fake.api(), runpod_cfg(list_staleness_s=0, gpu_type_ids=LIST),
                            clock, disk_gb_hint=20)  # fmt: skip
    conn = connect(tmp_path / "s.db")
    migrate(conn)
    rep = await lifecycle_probe(conn, adapter, b"x" * 32, clock, _cfg(tmp_path), image=SMALL,
                                gpu_type_id=G3090, max_price_usd_h=0.35, max_s=60, poll_s=5,
                                hold_s=0)  # fmt: skip
    assert rep.attempts == [(G3090, "13.0", "no_capacity"), (G3090, "12.9", "no_capacity"),
                            (G3090, "12.8", "no_capacity"), (G3090TI, "13.0", "no_capacity"),
                            (G3090TI, "12.9", "created")]  # fmt: skip
    assert rep.cost_per_hr == pytest.approx(0.275)
    assert rep.destroyed and rep.zero and fake.live_pods() == []


# ------------------------------------------------------------------ provisioner bookkeeping
async def test_provisioner_stores_the_created_type_and_its_price(tmp_path: Path) -> None:
    """The intent (`offer_json`) names the chosen offer; after create the row carries the
    GPU type and `costPerHr` of the pod actually created (the ledger reads `price_usd_h`)."""
    from harness import Harness, make_job

    h = Harness(tmp_path)
    real_create = h.provider.create

    async def create(spec: InstanceSpec, label: str) -> RemoteResource:
        res = await real_create(spec, label)
        return dataclasses.replace(res, offer_id="other-gpu", price_usd_h=0.34)

    h.provider.create = create  # type: ignore[method-assign]
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 30)
    (row,) = h.instances()
    offer = json.loads(row["offer_json"])
    assert offer["offer_id"] == "other-gpu" and offer["price_usd_h"] == 0.34
    assert row["price_usd_h"] == 0.34 and "disk_usd_h" in offer
