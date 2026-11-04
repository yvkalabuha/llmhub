"""T-372 fix round 1: adapter/core/probe/CLI fixes after review cycles 1 and 2.

REV-S4/R2-7 (DELETE 400/404 -> GET), REV-S10 (2xx create check: ref in the DB first, critical
incident, destroy), R2-10 (GraphQL `spendLimit` may not exist), R2-5 (fake shapes), R2-8
(weights-token-check: 403 is not proof by itself).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from harness import make_job
from llmhub.clock import FakeClock
from llmhub.provider_adapters.base import DestroyResult, ProviderError
from llmhub.provider_adapters.labels import make_label, new_op_id
from llmhub.provider_adapters.runpod import RunpodAdapter
from llmhub.provider_adapters.runpod_api import RunpodApi, RunpodHttpError
from runpod_fake import FAKE_KEY, FakeRunpod, make_adapter, make_spec, runpod_cfg
from test_t372_runpod_adapter import RunpodCoreHarness, core_cfg

SECRET = b"test-secret-0123456789abcdef-test-secret"


def _label() -> str:
    return make_label(new_op_id(), SECRET)


def _api(handler: Any) -> RunpodApi:
    return RunpodApi(lambda: FAKE_KEY, transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------ REV-S4 / R2-7
async def test_fake_delete_of_gone_pod_answers_400_and_destroy_is_already_gone() -> None:
    fake = FakeRunpod(FakeClock(), gone_delete_status=400)
    a = make_adapter(fake)
    res = await a.create(make_spec(), _label())
    assert await a.destroy(res.ref) == DestroyResult.ACCEPTED
    assert await a.destroy(res.ref) == DestroyResult.ALREADY_GONE
    assert await a.get(res.ref) is None
    statuses = [r.method for r in fake.requests if r.url.path == f"/v1/pods/{res.ref}"]
    assert statuses == ["DELETE", "DELETE", "GET", "GET"]


@pytest.mark.parametrize(("get_status", "pod"), [(200, "RUNNING"), (500, None)])
async def test_delete_400_of_a_live_pod_is_an_error(get_status: int, pod: str | None) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "DELETE":
            return httpx.Response(400, json={"error": "bad"})
        if get_status == 200:
            return httpx.Response(200, json={"id": "p1", "name": "x", "desiredStatus": pod})
        return httpx.Response(get_status)

    with pytest.raises(RunpodHttpError) as ei:
        await _api(handler).delete_pod("p1")
    assert ei.value.status == (400 if get_status == 200 else 500)


async def test_delete_400_of_a_terminated_pod_is_gone() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "DELETE":
            return httpx.Response(400)
        return httpx.Response(200, json={"id": "p1", "name": "x", "desiredStatus": "TERMINATED"})

    assert await _api(handler).delete_pod("p1") is False


@pytest.mark.parametrize(("listed", "gone"), [([], True), ([{"id": "p1", "name": "x",
                                                            "desiredStatus": "RUNNING"}], False)])  # fmt: skip
async def test_get_400_checked_against_the_list(listed: list[dict[str, Any]], gone: bool) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/pods":
            return httpx.Response(200, json=listed)
        return httpx.Response(400)

    api = _api(handler)
    if gone:
        assert await api.get_pod("p1") is None
    else:
        with pytest.raises(RunpodHttpError):
            await api.get_pod("p1")


# ------------------------------------------------------------------ R2-10
async def test_myself_without_spend_limit_field() -> None:
    fake = FakeRunpod(FakeClock(), has_spend_limit=False)
    me = await fake.api().myself()
    assert me.spend_limit is None and me.balance_usd == 25.0
    queries = [r.content for r in fake.requests if r.url.host == "api.runpod.io"]
    assert len(queries) == 2 and b"spendLimit" not in queries[1]
    assert (await FakeRunpod(FakeClock()).api().myself()).spend_limit == 80


async def test_myself_other_errors_still_raise() -> None:
    fake = FakeRunpod(FakeClock())
    fake.inject("POST", "graphql:myself", "status", status=401)
    with pytest.raises(RunpodHttpError):
        await fake.api().myself()


# ------------------------------------------------------------------ REV-S10 in the core
@pytest.mark.parametrize("extra", [{"volumeInGb": 20}, {"interruptible": True},
                                   {"networkVolume": {"id": "nv1"}}])  # fmt: skip
async def test_core_rejected_create_stores_ref_then_destroys(tmp_path: Path,
                                                             extra: dict[str, Any]) -> None:  # fmt: skip
    h = RunpodCoreHarness(tmp_path, core_cfg(tmp_path), create_answer_extra=extra)
    await h.drive(h.controller.start())
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 30)
    (row,) = h.instances()
    (pod,) = h.fake.pods.values()
    assert row["provider_ref"] == pod.id and pod.deleted_at is not None
    assert ("PROVISIONING", "ERROR", "created_resource_rejected") in h.transitions()
    (inc,) = h.incidents("created_resource_rejected")
    assert inc["severity"] == "critical" and pod.id in inc["details_json"]
    assert h.fake.posts() == 1


async def test_core_rejected_foreign_name_ref_kept_when_destroy_fails(tmp_path: Path) -> None:
    """The ref is in the DB BEFORE the destroy: a foreign-named pod stays findable. The core
    never destroys a ref under another label later (R1-11 `ownership_mismatch`): manual."""
    h = RunpodCoreHarness(tmp_path, core_cfg(tmp_path), name_override="not-ours")
    await h.drive(h.controller.start())
    h.fake.inject("DELETE", "/pods/", "status", status=500, times=2)
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 30)
    (row,) = h.instances()
    (pod,) = h.fake.pods.values()
    assert row["provider_ref"] == pod.id
    assert h.incidents("created_resource_rejected")[0]["severity"] == "critical"
    assert h.incidents("created_resource_destroy_failed")
    await h.run_until(t0 + 600)
    assert h.instances()[0]["provider_ref"] == pod.id and h.fake.posts() == 1
    assert h.incidents("ownership_mismatch")[0]["severity"] == "critical"


# ------------------------------------------------------------------ REV-S10 in the probe
async def test_probe_rejected_create_stores_ref_and_destroys(tmp_path: Path) -> None:
    from llmhub.persistence.db import connect, migrate
    from llmhub.tools.t372_ops import lifecycle_probe
    from test_review_t372_fn_runpod_real import SMALL, _AutoClock, _cfg

    clock = _AutoClock()
    fake = FakeRunpod(clock, create_answer_extra={"volumeInGb": 20})
    adapter = RunpodAdapter(fake.api(), runpod_cfg(list_staleness_s=0), clock, disk_gb_hint=20)
    conn = connect(tmp_path / "s.db")
    migrate(conn)
    rep = await lifecycle_probe(conn, adapter, b"x" * 32, clock, _cfg(tmp_path), image=SMALL,
                                gpu_type_id="NVIDIA GeForce RTX 3090", max_s=60, poll_s=5)  # fmt: skip
    (pod,) = fake.pods.values()
    assert rep.ref == pod.id and rep.destroyed and fake.live_pods() == []
    assert rep.error is not None and "volumeInGb" in rep.error
    row = conn.execute("SELECT provider_ref FROM instances").fetchone()
    assert row[0] == pod.id
    inc = conn.execute("SELECT severity FROM incidents WHERE kind='created_resource_rejected'"
                       ).fetchone()  # fmt: skip
    assert inc[0] == "critical"


async def test_probe_hold_and_timings(tmp_path: Path) -> None:
    from llmhub.persistence.db import connect, migrate
    from llmhub.tools.t372_ops import lifecycle_probe
    from test_review_t372_fn_runpod_real import SMALL, _AutoClock, _cfg

    clock = _AutoClock()
    fake = FakeRunpod(clock)
    adapter = RunpodAdapter(fake.api(), runpod_cfg(list_staleness_s=0), clock, disk_gb_hint=20)
    conn = connect(tmp_path / "s.db")
    migrate(conn)
    rep = await lifecycle_probe(conn, adapter, b"x" * 32, clock, _cfg(tmp_path), image=SMALL,
                                gpu_type_id="NVIDIA GeForce RTX 3090", max_s=300, poll_s=5,
                                hold_s=40)  # fmt: skip
    assert rep.desired_running_s is not None and rep.held_s is not None and rep.held_s >= 40
    assert rep.zero and fake.live_pods() == []


async def test_probe_negative_hold_refused(tmp_path: Path) -> None:
    from llmhub.persistence.db import connect, migrate
    from llmhub.tools.t372_ops import OpsError, lifecycle_probe
    from test_review_t372_fn_runpod_real import SMALL, _AutoClock, _cfg

    clock = _AutoClock()
    fake = FakeRunpod(clock)
    conn = connect(tmp_path / "s.db")
    migrate(conn)
    with pytest.raises(OpsError):
        await lifecycle_probe(conn, make_adapter(fake), b"x" * 32, clock, _cfg(tmp_path),
                              image=SMALL, gpu_type_id="x", hold_s=-1)  # fmt: skip
    assert fake.posts() == 0


async def test_destroy_error_paths_keep_provider_error() -> None:
    fake = FakeRunpod(FakeClock())
    fake.inject("DELETE", "/pods/", "status", status=401)
    with pytest.raises(ProviderError):
        await make_adapter(fake).destroy("abc")
