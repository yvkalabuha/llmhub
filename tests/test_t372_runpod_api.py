"""T-372 A2: RunpodApi parsing, auth header, 404 handling, GraphQL errors, shapes, pages."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from llmhub.clock import FakeClock
from llmhub.provider_adapters.runpod_api import (
    RunpodApi,
    RunpodHttpError,
    RunpodParseError,
    _Parser,
    parse_pod,
    parse_time,
)
from runpod_fake import FAKE_KEY, GPU_ID, FakeRunpod

FIX = Path(__file__).parent / "fixtures" / "runpod"
P = _Parser("GET", "/pods")


def _fix(name: str) -> dict[str, Any]:
    return json.loads((FIX / "synthetic" / name).read_text())


def test_parse_running_pod_drops_env() -> None:
    raw = _fix("pod_running.json")
    pod = parse_pod(raw, P)
    assert (pod.id, pod.desired_status, pod.cost_per_hr) == ("abc123xyz0001", "RUNNING", 0.225)
    assert pod.machine_id == "m1x2y3" and pod.image and pod.image.endswith("ab")
    assert pod.created_at == parse_time("2026-11-04T10:00:00Z")
    assert not hasattr(pod, "env")
    assert "SYNTHETIC-SECRET-VALUE" not in repr(pod)


def test_parse_exited_and_pending() -> None:
    ex = parse_pod(_fix("pod_exited.json"), P)
    assert ex.desired_status == "EXITED" and ex.created_at == parse_time("2026-11-04T10:00:00.123Z")
    pe = parse_pod(_fix("pod_pending.json"), P)  # costPerHr as a numeric string (openapi example)
    assert pe.machine_id is None and pe.image is not None and pe.created_at == 1793786400.0
    assert pe.cost_per_hr == 0.225


def test_parse_money_fields_and_missing_start() -> None:
    """R2-3/R2-9/REV-S10: created_at = lastStartedAt (absent while pulling), costPerHr may be
    a numeric string, the create-check fields are parsed."""
    raw = {**_fix("pod_running.json"), "networkVolume": {"id": "nv1", "size": 10}}
    del raw["lastStartedAt"]
    pod = parse_pod(raw, P)
    assert pod.created_at is None and pod.volume_in_gb == 0 and pod.interruptible is False
    assert pod.network_volume_id == "nv1"
    bare = parse_pod({"id": "x1", "name": "n", "desiredStatus": "RUNNING", "lastStartedAt": ""},
                     P)  # fmt: skip
    assert bare.created_at is None and bare.cost_per_hr is None and bare.volume_in_gb is None
    assert parse_pod({**raw, "networkVolume": {}}, P).network_volume_id is None


@pytest.mark.parametrize("patch", [{"volumeInGb": "0"}, {"interruptible": "no"},
                                   {"networkVolume": 5}, {"networkVolume": {"id": 7}}])  # fmt: skip
def test_odd_money_field_types_never_blind_the_list(patch: dict[str, Any]) -> None:
    """A foreign pod with an odd field must not break `list_pods`; after OUR create the same
    oddity is a mismatch (`created_mismatch`)."""
    from llmhub.provider_adapters.runpod import created_mismatch

    pod = parse_pod({**_fix("pod_running.json"), **patch}, P)
    assert pod.odd_fields == (next(iter(patch)),)
    assert created_mismatch(pod, pod.name, 0.3) == f"type of {next(iter(patch))}"


@pytest.mark.parametrize(
    "patch",
    [{"id": 5}, {"id": ""}, {"name": None}, {"desiredStatus": 1}, {"costPerHr": "cheap"},
     {"costPerHr": True}, {"costPerHr": "nan"}, {"lastStartedAt": "yesterday"},
     {"machineId": 3}],
)  # fmt: skip
def test_parse_unexpected_shape(patch: dict[str, Any]) -> None:
    raw = {**_fix("pod_running.json"), **patch}
    with pytest.raises(RunpodParseError):
        parse_pod(raw, P)


def _api(handler: Any) -> RunpodApi:
    return RunpodApi(lambda: FAKE_KEY, transport=httpx.MockTransport(handler))


async def test_bearer_header_and_no_key_in_url() -> None:
    fake = FakeRunpod(FakeClock())
    api = fake.api()
    await api.list_pods()
    await api.myself()
    await api.gpu_types([GPU_ID])
    for r in fake.requests:
        assert r.headers["authorization"] == f"Bearer {FAKE_KEY}"
        assert FAKE_KEY not in str(r.url) and "api_key" not in str(r.url)
    await api.aclose()


async def test_error_text_has_no_key_and_no_body() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"internal error for {FAKE_KEY} BODYTEXT")

    with pytest.raises(RunpodHttpError) as ei:
        await _api(handler).list_pods()
    s = str(ei.value)
    assert s == "GET /pods -> 500" and FAKE_KEY not in s and "BODYTEXT" not in s
    assert ei.value.sent and ei.value.status == 500


async def test_404_is_none_and_false() -> None:
    fake = FakeRunpod(FakeClock())
    api = fake.api()
    assert await api.get_pod("nosuchpod1") is None
    assert await api.delete_pod("nosuchpod1") is False


async def test_bad_pod_id_refused_without_request() -> None:
    fake = FakeRunpod(FakeClock())
    with pytest.raises(RunpodHttpError, match="bad_id") as ei:
        await fake.api().get_pod("../templates")
    assert not ei.value.sent and fake.requests == []


async def test_graphql_errors_at_200() -> None:
    fake = FakeRunpod(FakeClock())
    fake.inject("POST", "graphql:gpuTypes", "graphql_error", body="Insufficient balance")
    with pytest.raises(RunpodHttpError) as ei:
        await fake.api().gpu_types([GPU_ID])
    assert ei.value.status == 200 and ei.value.code == "insufficient_balance"
    assert "Insufficient" not in str(ei.value)


async def test_unexpected_shape_raises_parse_error() -> None:
    fake = FakeRunpod(FakeClock())
    fake.inject("GET", "/pods", "shape")
    with pytest.raises(RunpodParseError):
        await fake.api().list_pods()

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    with pytest.raises(RunpodParseError):
        await _api(handler).list_pods()


async def test_second_page_failure_raises() -> None:
    clock = FakeClock()
    fake = FakeRunpod(clock, paged=True)
    api = fake.api()
    fake.add_pod("llmhub-000000000001-00000000")
    fake.add_pod("llmhub-000000000002-00000000")
    assert len(await api.list_pods()) == 2  # both pages
    fake.inject("GET", "/pods", "page2")  # page 1 answers, page 2 -> 503
    with pytest.raises(RunpodHttpError):
        await api.list_pods()


async def test_connect_error_not_sent_timeout_sent() -> None:
    fake = FakeRunpod(FakeClock())
    fake.inject("GET", "/pods", "connect")
    with pytest.raises(RunpodHttpError) as ei:
        await fake.api().list_pods()
    assert not ei.value.sent and ei.value.code == "connect"
    fake.inject("POST", "/pods", "read_timeout")
    with pytest.raises(RunpodHttpError) as ei2:
        await fake.api().create_pod({"name": "x", "imageName": "y"})
    assert ei2.value.sent and ei2.value.code == "timeout" and fake.pods == {}


async def test_key_reader_failure_is_not_sent() -> None:
    def boom() -> str:
        raise RuntimeError("cannot read /run/secrets/runpod: rpa_LEAK")

    api = RunpodApi(boom, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    with pytest.raises(RunpodHttpError) as ei:
        await api.list_pods()
    assert ei.value.code == "key_unavailable" and not ei.value.sent
    assert "rpa_LEAK" not in str(ei.value) and ei.value.__cause__ is None


async def test_billing_and_myself_and_gpu_types() -> None:
    clock = FakeClock()
    fake = FakeRunpod(clock)
    api = fake.api()
    pod = await api.create_pod({"name": "llmhub-000000000001-00000000", "imageName": "i@x",
                                "env": {"A": "B"}})  # fmt: skip
    clock.advance_to(clock.now() + 1800)
    me = await api.myself()
    assert me.balance_usd == pytest.approx(25 - 0.225 / 2) and me.spend_per_hr == 0.225
    buckets = await api.billing_pods(clock.now() - 7200, clock.now())
    assert {b.pod_id for b in buckets} == {pod.id}
    assert sum(b.amount_usd for b in buckets) == pytest.approx(0.225 / 2, abs=1e-5)
    assert all(b.end - b.start == 3600 for b in buckets)
    (g,) = await api.gpu_types([GPU_ID])
    assert (g.community_price, g.stock_status, g.memory_gb) == (0.22, "High", 24)
    fake.stock = None  # a hint only (R3-3): the Community price stays
    (g2,) = await api.gpu_types([GPU_ID])
    assert g2.community_price == 0.22 and g2.stock_status is None
    fake.community = False
    (g3,) = await api.gpu_types([GPU_ID])
    assert g3.community_price is None


async def test_lists_of_volumes_and_templates() -> None:
    fake = FakeRunpod(FakeClock())
    fake.volumes.append({"id": "v1", "name": "llmhub-x", "size": 10})
    fake.templates.append({"id": "t1", "name": "mine", "imageName": "x"})
    api = fake.api()
    assert [(v.id, v.name) for v in await api.list_network_volumes()] == [("v1", "llmhub-x")]
    assert [(t.id, t.name) for t in await api.list_templates()] == [("t1", "mine")]
    (req,) = [r for r in fake.requests if r.url.path == "/v1/templates"]
    assert "includePublicTemplates=false" in str(req.url)


def test_no_io_at_construction() -> None:
    api = RunpodApi(lambda: (_ for _ in ()).throw(AssertionError("key read")))
    assert api._client is None and "rest.runpod.io" in repr(api)


@pytest.mark.parametrize("path", sorted((FIX / "recorded").glob("*.json")), ids=lambda p: p.name)
def test_recorded_fixtures_parse(path: Path) -> None:
    """A2b: answers recorded in L1 (allowlisted fields only) parse without errors."""
    doc = json.loads(path.read_text())
    kind = doc.get("_kind")
    items = doc.get("items", [])
    if kind == "pods":
        for it in items:
            parse_pod(it, P)
    elif kind in ("networkvolumes", "templates"):
        for it in items:
            assert isinstance(it.get("id"), str) and isinstance(it.get("name"), str)
