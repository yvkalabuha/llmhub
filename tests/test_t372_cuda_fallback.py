"""T-372 L2b finding: the real "no instances currently available" 500 is a definite refusal,
and create tries the allowed CUDA versions one at a time, highest first (fake RunPod only)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llmhub.clock import FakeClock
from llmhub.provider_adapters.base import ProviderError, UnavailableReason
from llmhub.provider_adapters.labels import make_label, new_op_id
from llmhub.provider_adapters.runpod import RunpodAdapter, cuda_order, normalize_runpod_error
from llmhub.provider_adapters.runpod_api import RunpodHttpError, classify
from runpod_fake import GPU_ID as G
from runpod_fake import NO_INSTANCES_BODY, FakeRunpod, make_adapter, make_spec, runpod_cfg

SECRET = b"test-secret-0123456789abcdef-test-secret"


def _label() -> str:
    return make_label(new_op_id(), SECRET)


def _cudas(fake: FakeRunpod) -> list[list[str]]:
    return [b["allowedCudaVersions"] for b in fake.created_bodies]


def _gets(fake: FakeRunpod) -> int:
    return sum(1 for r in fake.requests if r.method == "GET" and r.url.path == "/v1/pods")


# ------------------------------------------------------------------ classification
def test_real_no_instances_500_body_is_no_capacity() -> None:
    assert classify(NO_INSTANCES_BODY, status=500) == "no_capacity"
    upper = json.dumps({"error": "create pod: THERE ARE NO INSTANCES CURRENTLY AVAILABLE"})
    assert classify(upper, status=500) == "no_capacity"


@pytest.mark.parametrize(
    ("text", "status"),
    [
        (NO_INSTANCES_BODY, 502),  # not the documented 500
        (NO_INSTANCES_BODY, 503),
        ('{"error":"create pod: internal error","status":500}', 500),
        ('{"error": 5}', 500),
        ("not json at all", 500),
    ],
)
def test_other_5xx_stays_unknown(text: str, status: int) -> None:
    code = classify(text, status=status)
    err = normalize_runpod_error(RunpodHttpError(method="POST", path="/pods", status=status,
                                                 code=code, retry_after_s=None, sent=True))  # fmt: skip
    assert err.outcome_unknown and err.reason == UnavailableReason.PROVIDER_API_ERROR


def test_no_instances_error_is_definite_gpu_unavailable_short_message() -> None:
    code = classify(NO_INSTANCES_BODY, status=500)
    err = normalize_runpod_error(RunpodHttpError(method="POST", path="/pods", status=500,
                                                 code=code, retry_after_s=None, sent=True))  # fmt: skip
    assert err.reason == UnavailableReason.GPU_UNAVAILABLE and not err.outcome_unknown
    assert err.message == "POST /pods -> 500 no_capacity"  # no body in the message


def test_cuda_order_numeric_desc() -> None:
    assert cuda_order(["12.8", "12.9", "13.0"]) == ["13.0", "12.9", "12.8"]
    assert cuda_order(["12.9", "12.10", "12.8"]) == ["12.10", "12.9", "12.8"]
    assert cuda_order(["12.8", "12.8"]) == ["12.8"]


# ------------------------------------------------------------------ the fake models the real API
async def test_fake_multi_version_list_narrows_like_the_real_api() -> None:
    fake = FakeRunpod(FakeClock(), cuda_capacity={"13.0"})
    body = {"name": _label(), "imageName": "x", "allowedCudaVersions": ["12.8", "12.9", "13.0"]}
    with pytest.raises(RunpodHttpError) as ei:
        await fake.api().create_pod(body)
    assert ei.value.status == 500 and ei.value.code == "no_capacity" and not fake.pods
    body.pop("allowedCudaVersions")
    pod = await fake.api().create_pod(body)
    assert pod.id in fake.pods


# ------------------------------------------------------------------ create: one version per POST
async def test_create_starts_with_the_highest_version() -> None:
    fake = FakeRunpod(FakeClock(), cuda_capacity={"13.0", "12.9", "12.8"})
    a = make_adapter(fake)
    res = await a.create(make_spec(), _label())
    assert _cudas(fake) == [["13.0"]] and fake.posts() == 1
    assert a.last_create_attempts == [(G, "13.0", "created")] and res.ref in fake.pods


async def test_create_moves_down_on_no_capacity_only() -> None:
    fake = FakeRunpod(FakeClock(), cuda_capacity={"12.8"})
    a = make_adapter(fake)
    res = await a.create(make_spec(), _label())
    assert _cudas(fake) == [["13.0"], ["12.9"], ["12.8"]]
    assert a.last_create_attempts == [(G, "13.0", "no_capacity"), (G, "12.9", "no_capacity"),
                                      (G, "12.8", "created")]  # fmt: skip
    assert [p.id for p in fake.live_pods()] == [res.ref]
    assert _gets(fake) == 1  # max_live_pods checked once, before the first POST


async def test_all_versions_refused_is_definite_gpu_unavailable() -> None:
    fake = FakeRunpod(FakeClock(), cuda_capacity=set())
    a = make_adapter(fake)
    with pytest.raises(ProviderError) as ei:
        await a.create(make_spec(), _label())
    assert ei.value.err.reason == UnavailableReason.GPU_UNAVAILABLE
    assert not ei.value.err.outcome_unknown and ei.value.err.retryable
    assert fake.posts() == 3 and not fake.pods
    assert [r for *_, r in a.last_create_attempts] == ["no_capacity"] * 3


@pytest.mark.parametrize("kind", ["timeout_after", "read_timeout"])
async def test_unknown_after_no_capacity_stops_at_once(kind: str) -> None:
    fake = FakeRunpod(FakeClock(), cuda_capacity={"12.9", "12.8"})
    fake.inject("POST", "/pods", "status", status=500, body=NO_INSTANCES_BODY)
    fake.inject("POST", "/pods", kind)
    a = make_adapter(fake)
    with pytest.raises(ProviderError) as ei:
        await a.create(make_spec(), _label())
    assert ei.value.err.outcome_unknown and fake.posts() == 2
    assert a.last_create_attempts == [(G, "13.0", "no_capacity"), (G, "12.9", "unknown")]


async def test_other_500_on_first_post_stops_unknown() -> None:
    fake = FakeRunpod(FakeClock())
    fake.inject("POST", "/pods", "status", status=500,
                body='{"error":"create pod: something broke","status":500}')  # fmt: skip
    a = make_adapter(fake)
    with pytest.raises(ProviderError) as ei:
        await a.create(make_spec(), _label())
    assert ei.value.err.outcome_unknown and fake.posts() == 1
    assert a.last_create_attempts == [(G, "13.0", "unknown")]


async def test_other_definite_refusal_stops_without_next_version() -> None:
    fake = FakeRunpod(FakeClock())
    fake.inject("POST", "/pods", "status", status=400, body='{"error":"bad request"}')
    a = make_adapter(fake)
    with pytest.raises(ProviderError) as ei:
        await a.create(make_spec(), _label())
    assert not ei.value.err.outcome_unknown and fake.posts() == 1
    assert a.last_create_attempts == [(G, "13.0", "deployment_error")]


async def test_max_posts_is_the_number_of_versions() -> None:
    fake = FakeRunpod(FakeClock(), cuda_capacity=set())
    a = make_adapter(fake, allowed_cuda_versions=["13.0"])
    with pytest.raises(ProviderError):
        await a.create(make_spec(), _label())
    assert fake.posts() == 1


async def test_precheck_refusal_has_no_attempts() -> None:
    fake = FakeRunpod(FakeClock())
    fake.add_pod("llmhub-000000000000-00000000")
    a = make_adapter(fake)
    with pytest.raises(ProviderError):
        await a.create(make_spec(), _label())
    assert fake.posts() == 0 and a.last_create_attempts == []


# ------------------------------------------------------------------ lifecycle-probe
async def test_probe_reports_attempts(tmp_path: Path) -> None:
    from llmhub.persistence.db import connect, migrate
    from llmhub.tools.t372_ops import lifecycle_probe
    from test_review_t372_fn_runpod_real import SMALL, _AutoClock, _cfg

    clock = _AutoClock()
    fake = FakeRunpod(clock, cuda_capacity={"12.9"})
    adapter = RunpodAdapter(fake.api(), runpod_cfg(list_staleness_s=0), clock, disk_gb_hint=20)
    conn = connect(tmp_path / "s.db")
    migrate(conn)
    rep = await lifecycle_probe(conn, adapter, b"x" * 32, clock, _cfg(tmp_path), image=SMALL,
                                gpu_type_id="NVIDIA GeForce RTX 3090", max_s=60, poll_s=5,
                                hold_s=0)  # fmt: skip
    assert rep.attempts == [(G, "13.0", "no_capacity"), (G, "12.9", "created")]
    assert rep.destroyed and rep.zero and fake.live_pods() == []


async def test_probe_all_refused_is_no_capacity_not_unknown(tmp_path: Path) -> None:
    from llmhub.persistence.db import connect, migrate
    from llmhub.tools.t372_ops import lifecycle_probe
    from test_review_t372_fn_runpod_real import SMALL, _AutoClock, _cfg

    clock = _AutoClock()
    fake = FakeRunpod(clock, cuda_capacity=set())
    adapter = RunpodAdapter(fake.api(), runpod_cfg(list_staleness_s=0), clock, disk_gb_hint=20)
    conn = connect(tmp_path / "s.db")
    migrate(conn)
    rep = await lifecycle_probe(conn, adapter, b"x" * 32, clock, _cfg(tmp_path), image=SMALL,
                                gpu_type_id="NVIDIA GeForce RTX 3090", max_s=60, poll_s=5)  # fmt: skip
    assert [r for *_, r in rep.attempts] == ["no_capacity"] * 3
    assert rep.error is not None and "no_capacity" in rep.error
    assert not any("unknown" in n for n in rep.notes) and fake.posts() == 3
