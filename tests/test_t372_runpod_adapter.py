"""T-372 A4: RunpodAdapter safety rules (plan 3.3) on the fake RunPod (no network)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from harness import TEST_SECRET, Harness, make_job
from llmhub.clock import FakeClock
from llmhub.cloud_manager.inference import JobRunResult, JobRunSpec
from llmhub.configuration.models import Config
from llmhub.controller import Controller
from llmhub.provider_adapters.base import (
    Capability,
    DestroyResult,
    OfferQuery,
    ProviderError,
    RemoteStatus,
    ResourceKind,
    UnavailableReason,
)
from llmhub.provider_adapters.labels import make_label, new_op_id
from llmhub.provider_adapters.runpod import RunpodAdapter
from llmhub.reaper.core import Reaper
from runpod_fake import GPU_ID, IMAGE, FakeRunpod, make_adapter, make_spec

SECRET = b"test-secret-0123456789abcdef-test-secret"


def _label() -> str:
    return make_label(new_op_id(), SECRET)


def _setup(**fake_kw: object) -> tuple[FakeRunpod, FakeClock]:
    clock = FakeClock()
    return FakeRunpod(clock, **fake_kw), clock  # type: ignore[arg-type]


async def test_create_body_and_resource() -> None:
    fake, _ = _setup()
    a = make_adapter(fake)
    spec = make_spec(secret_env={"LLMHUB_API_KEY": "pod-secret-1"})
    label = _label()
    res = await a.create(spec, label)
    (body,) = fake.created_bodies
    for banned in ("networkVolumeId", "templateId", "containerRegistryAuthId"):
        assert banned not in body
    assert body["name"] == label and body["imageName"] == spec.image
    assert body["cloudType"] == "COMMUNITY" and body["computeType"] == "GPU"
    assert body["gpuTypeIds"] == [GPU_ID] and body["gpuCount"] == 1
    assert body["allowedCudaVersions"] == ["12.8", "12.9"]
    assert body["volumeInGb"] == 0 and body["containerDiskInGb"] == 40
    assert body["interruptible"] is False and body["ports"] == ["8080/http"]
    assert body["env"]["LLMHUB_API_KEY"] == "pod-secret-1"  # secrets reach the pod only
    assert res.label == label and res.kind == ResourceKind.INSTANCE
    assert res.endpoint == f"https://{res.ref}-8080.proxy.runpod.net"
    assert res.status == RemoteStatus.RUNNING and res.host_id and res.spend_rate_usd_h == 0.225
    assert "pod-secret-1" not in repr(res)
    assert "pod-secret-1" not in repr(await a.get(res.ref))


@pytest.mark.parametrize("kind", ["timeout_after", "read_timeout"])
async def test_one_post_on_timeout_outcome_unknown(kind: str) -> None:
    fake, _ = _setup()
    fake.inject("POST", "/pods", kind)
    with pytest.raises(ProviderError) as ei:
        await make_adapter(fake).create(make_spec(), _label())
    assert ei.value.err.outcome_unknown and fake.posts() == 1


async def test_5xx_on_create_unknown_connect_error_definite() -> None:
    fake, _ = _setup()
    fake.inject("POST", "/pods", "status", status=502, body="bad gateway")
    with pytest.raises(ProviderError) as ei:
        await make_adapter(fake).create(make_spec(), _label())
    assert ei.value.err.outcome_unknown and fake.posts() == 1
    fake.inject("POST", "/pods", "connect")
    with pytest.raises(ProviderError) as ei2:
        await make_adapter(fake).create(make_spec(), _label())
    assert not ei2.value.err.outcome_unknown


async def test_no_capacity_is_gpu_unavailable() -> None:
    fake, _ = _setup()
    fake.inject("POST", "/pods", "status", status=500,
                body="There are no longer any instances available")  # fmt: skip
    with pytest.raises(ProviderError) as ei:
        await make_adapter(fake).create(make_spec(), _label())
    assert ei.value.err.reason == UnavailableReason.GPU_UNAVAILABLE
    assert not ei.value.err.outcome_unknown


@pytest.mark.parametrize(
    ("spec_over", "label_ok"),
    [
        ({"image": "ghcr.io/yvkalabuha/llmhub-infer:0.3.0"}, True),
        ({"image": "ghcr.io/x@sha256:" + "z" * 64}, True),
        ({"env": {"LLMHUB_TRANSPORT": "direct"}}, True),
        ({"offer_id": "NVIDIA H100 80GB HBM3"}, True),
        ({}, False),
    ],
)
async def test_precheck_refusals_send_nothing(spec_over: dict[str, object], label_ok: bool) -> None:
    fake, _ = _setup()
    with pytest.raises(ProviderError) as ei:
        await make_adapter(fake).create(make_spec(**spec_over),
                                        _label() if label_ok else "my-pod")  # fmt: skip
    assert ei.value.err.reason == UnavailableReason.DEPLOYMENT_ERROR
    assert not ei.value.err.outcome_unknown and fake.posts() == 0
    assert fake.requests == []


async def test_price_above_ceiling_refused() -> None:
    fake, _ = _setup(price=0.31)
    with pytest.raises(ProviderError) as ei:
        await make_adapter(fake).create(make_spec(max_price_usd_h=0.30), _label())
    assert ei.value.err.reason == UnavailableReason.DEPLOYMENT_ERROR and fake.posts() == 0
    fake2, _ = _setup(price=0.25)
    with pytest.raises(ProviderError):  # section ceiling below the pool price
        await make_adapter(fake2, price_ceiling_usd_h=0.20).create(make_spec(), _label())
    assert fake2.posts() == 0


async def test_out_of_stock_refused() -> None:
    fake, _ = _setup(stock=None)
    with pytest.raises(ProviderError) as ei:
        await make_adapter(fake).create(make_spec(), _label())
    assert ei.value.err.reason == UnavailableReason.GPU_UNAVAILABLE and fake.posts() == 0


@pytest.mark.parametrize("status", ["RUNNING", "EXITED", "CREATED", "RESTARTING"])
async def test_max_live_pods_counts_every_status_but_terminated(status: str) -> None:
    """critic F6: an EXITED/CREATED/RESTARTING llmhub pod blocks a new create."""
    fake, _ = _setup()
    fake.add_pod(_label(), status=status)
    with pytest.raises(ProviderError) as ei:
        await make_adapter(fake).create(make_spec(), _label())
    assert ei.value.err.reason == UnavailableReason.QUOTA_EXCEEDED
    assert not ei.value.err.outcome_unknown and fake.posts() == 0


async def test_terminated_and_foreign_pods_do_not_count() -> None:
    fake, _ = _setup()
    fake.add_pod(_label(), status="TERMINATED")
    fake.add_pod("my-jupyter", status="RUNNING")
    await make_adapter(fake).create(make_spec(), _label())
    assert fake.posts() == 1


async def test_max_live_pods_two() -> None:
    fake, _ = _setup()
    fake.add_pod(_label(), status="EXITED")
    a = make_adapter(fake, max_live_pods=2)
    await a.create(make_spec(), _label())
    with pytest.raises(ProviderError):
        await a.create(make_spec(), _label())
    assert fake.posts() == 1


@pytest.mark.parametrize("fault", ["connect", "status", "shape"])
async def test_list_failure_refuses_create(fault: str) -> None:
    fake, _ = _setup()
    fake.inject("GET", "/pods", fault, status=503)
    with pytest.raises(ProviderError) as ei:
        await make_adapter(fake).create(make_spec(), _label())
    assert not ei.value.err.outcome_unknown and fake.posts() == 0


@pytest.mark.parametrize(("over", "what"), [({"name_override": "llmhub-other"}, "name"),
                                            ({"cost_override": 0.45}, "price")])  # fmt: skip
async def test_mismatch_after_create_deletes_pod(over: dict[str, object], what: str) -> None:
    fake, _ = _setup(**over)
    with pytest.raises(ProviderError) as ei:
        await make_adapter(fake).create(make_spec(), _label())
    assert ei.value.err.outcome_unknown and what in ei.value.err.message
    assert "accepted" in ei.value.err.message
    assert fake.posts() == 1 and fake.live_pods() == []


async def test_destroy_pods_only() -> None:
    fake, _ = _setup()
    a = make_adapter(fake)
    for ref in ("vol:v1", "tpl:t1"):
        with pytest.raises(ProviderError) as ei:
            await a.destroy(ref)
        assert ei.value.err.reason == UnavailableReason.DEPLOYMENT_ERROR
        assert not ei.value.err.retryable
    assert fake.requests == []
    res = await a.create(make_spec(), _label())
    assert await a.destroy(res.ref) == DestroyResult.ACCEPTED
    assert await a.destroy(res.ref) == DestroyResult.ALREADY_GONE
    assert await a.get(res.ref) is None


async def test_list_owned_kinds_and_filters() -> None:
    fake, _ = _setup()
    a = make_adapter(fake)
    res = await a.create(make_spec(), _label())
    fake.add_pod("someone-else")
    fake.add_pod(_label(), status="TERMINATED")
    fake.volumes += [{"id": "v1", "name": "llmhub-vol"}, {"id": "v2", "name": "data"}]
    fake.templates += [{"id": "t1", "name": "llmhub-tpl"}]
    owned = await a.list_owned("llmhub-")
    kinds = sorted((r.kind.value, r.ref) for r in owned)
    assert kinds == [("instance", res.ref), ("other", "tpl:t1"), ("volume", "vol:v1")]
    only = await a.list_owned(res.label)
    assert [r.ref for r in only] == [res.ref]
    assert (await a.get("vol:v1")) is not None and await a.get("vol:none") is None


@pytest.mark.parametrize(
    ("method", "path", "kind"),
    [("GET", "/pods", "status"), ("GET", "/pods", "page2"), ("GET", "/pods", "shape"),
     ("GET", "/networkvolumes", "status"), ("GET", "/templates", "connect"),
     ("GET", "/templates", "shape")],
)  # fmt: skip
async def test_list_owned_failure_raises(method: str, path: str, kind: str) -> None:
    fake, _ = _setup(paged=kind == "page2")
    a = make_adapter(fake)
    await a.create(make_spec(), _label())
    fake.add_pod(_label(), status="EXITED")
    fake.inject(method, path, kind, status=500)
    with pytest.raises(ProviderError):
        await a.list_owned("llmhub-")


async def test_offers_balance_billing() -> None:
    fake, clock = _setup()
    a = make_adapter(fake)
    assert a.capabilities & Capability.BALANCE_API and a.capabilities & Capability.BILLING_API
    assert not a.capabilities & Capability.NATIVE_LABELS
    q = OfferQuery(gpu="RTX3090", min_vram_gb=24, max_price_usd_h=0.3, exclude_hosts=frozenset())
    (o,) = await a.list_offers(q)
    assert (o.offer_id, o.price_usd_h, o.vram_gb, o.host_id) == (GPU_ID, 0.22, 24, "")
    assert o.disk_usd_h == pytest.approx(40 * 0.10 / 730)
    assert await a.list_offers(OfferQuery("H100", 80, 1.0, frozenset())) == []
    fake.stock = None
    assert await a.list_offers(q) == []
    fake.stock = "Low"
    res = await a.create(make_spec(), _label())
    clock.advance_to(clock.now() + 3600)
    bal = await a.balance()
    assert bal is not None and bal.usd == pytest.approx(25 - 0.225)
    recs = await a.billing_since(clock.now() - 7200)
    assert {r.ref for r in recs} == {res.ref}
    assert sum(r.amount_usd for r in recs) == pytest.approx(0.225, abs=1e-4)
    assert a.list_staleness_s == 30


async def test_exited_pod_maps_and_has_no_spend() -> None:
    fake, _ = _setup()
    a = make_adapter(fake)
    pid = fake.add_pod(_label(), status="EXITED")
    res = await a.get(pid)
    assert res is not None and res.status == RemoteStatus.EXITED and res.spend_rate_usd_h is None


# ======================================================================== A6: core on the fake

_EP = re.compile(r"^https://([^-]+)-8080\.proxy\.runpod\.net$")


class PodInference:
    """InferenceBackend stub: the pod answers once the fake shows it RUNNING."""

    def __init__(self, fake: FakeRunpod) -> None:
        self.fake = fake

    def _up(self, endpoint: str) -> bool:
        m = _EP.match(endpoint)
        pod = self.fake.pods.get(m.group(1)) if m else None
        return pod is not None and pod.deleted_at is None and self.fake._status(pod) == "RUNNING"

    async def ping(self, endpoint: str) -> bool:
        return self._up(endpoint)

    async def health(self, endpoint: str) -> bool:
        return self._up(endpoint)

    async def test_request(self, endpoint: str) -> bool:
        return self._up(endpoint)

    async def run(self, endpoint: str, job: JobRunSpec) -> JobRunResult:
        return JobRunResult(job.tokens_in, 10, "stop", b'{"ok":true}')


def core_cfg(tmp: Path, **timeouts: Any) -> Config:
    return Config.model_validate({
        "data_dir": tmp,
        "pools": {"p1": {
            "name": "p1", "provider": "runpod", "gpu": "RTX3090", "vram_gb": 24,
            "pp_tps": 1200, "tg_tps": 78, "image": IMAGE, "disk_gb": 40,
            "max_price_usd_h": 0.30, "models": ["m1"],
        }},
        "providers": {"enabled": ["runpod"], "runpod": {"list_staleness_s": 30}},
        "timeouts": {"create_visibility_grace_s": 600, "provisioning_s": 1200,
                     "offer_wait_s": 300, **timeouts},
        "retry": {"create_unknown_max_retries": 0},
    })  # fmt: skip


class RunpodCoreHarness(Harness):
    """The stream F harness with RunpodAdapter on the fake instead of MockProvider."""

    def __init__(self, data_dir: Path, cfg: Config, **fake_kw: Any) -> None:
        self.rp: RunpodAdapter | None = None
        super().__init__(data_dir, cfg)
        self.fake = FakeRunpod(self.clock, **fake_kw)
        rp_cfg = cfg.providers.runpod
        assert rp_cfg is not None
        self.rp = RunpodAdapter(self.fake.api(), rp_cfg, self.clock, disk_gb_hint=40)
        self.pod_inf = PodInference(self.fake)
        self.controller = self._make_controller()

    def _make_controller(self) -> Controller:
        if self.rp is None:
            return super()._make_controller()
        self._boots += 1
        return Controller(
            self.conn, self.cfg, self.clock, {"runpod": self.rp}, {"runpod": self.pod_inf},
            self.bodies, TEST_SECRET, boot_id=f"boot-{self._boots}",
        )  # fmt: skip

    def reaper(self, **kw: Any) -> Reaper:
        from llmhub.persistence.db import connect

        assert self.rp is not None
        conn = connect(self.db_path, busy_timeout_ms=self.cfg.db.reaper_busy_timeout_ms)
        return Reaper(conn, self.cfg, {"runpod": self.rp}, self.clock, TEST_SECRET, **kw)

    def ours(self) -> list[str]:
        return [p.id for p in self.fake.live_pods() if p.name.startswith("llmhub-")]


async def _start(h: RunpodCoreHarness) -> None:
    await h.drive(h.controller.start())


async def test_core_full_cycle_to_zero(tmp_path: Path) -> None:
    h = RunpodCoreHarness(tmp_path, core_cfg(tmp_path), destroy_lag_s=20)
    await _start(h)
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 120)
    assert h.jobs()[0]["status"] == "completed" and h.fake.posts() == 1
    await h.run_until(t0 + 1500)
    assert h.state_of() == "STOPPED" and h.ours() == []
    zero = h.conn.execute("SELECT * FROM transitions WHERE to_state='STOPPED'").fetchall()
    assert zero  # confirmed only after two empty lists >= list_staleness_s apart
    # the estimate never undercounts: it runs to zero_confirmed_at and adds the offer's disk
    # rate on top of costPerHr (open question of plan 10, settled in L2b)
    led, billed = h.ledger_total(), h.fake.billed()
    assert billed <= led <= billed * 1.25


async def test_core_kill_destroys_only_hmac_valid(tmp_path: Path) -> None:
    h = RunpodCoreHarness(tmp_path, core_cfg(tmp_path))
    await _start(h)
    t0 = h.clock.now()
    h.submit(make_job(0, max_tokens=100_000))
    await h.run_until(t0 + 30)
    bad_mac = h.fake.add_pod("llmhub-0123456789ab-deadbeef")
    foreign = h.fake.add_pod("my-own-pod")
    report = await h.drive(h.controller.kill())
    assert report.completed
    assert sorted(p.id for p in h.fake.live_pods()) == sorted([bad_mac, foreign])


async def test_core_reaper_flags_pod_without_row(tmp_path: Path) -> None:
    from llmhub.provider_adapters.labels import make_label as mk

    h = RunpodCoreHarness(tmp_path, core_cfg(tmp_path))
    orphan = h.fake.add_pod(mk(new_op_id(), TEST_SECRET))
    await h.drive(h.reaper().run_once())
    inc = h.conn.execute("SELECT * FROM incidents WHERE kind='unknown_owned_resource'"
                         ).fetchall()  # fmt: skip
    assert inc and inc[0]["ref"] == orphan


async def test_core_f1_late_pod_adopted_without_second_post(tmp_path: Path) -> None:
    h = RunpodCoreHarness(tmp_path, core_cfg(tmp_path), visible_after_s=150)
    await _start(h)
    h.fake.inject("POST", "/pods", "timeout_after")
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 100)
    assert h.fake.posts() == 1 and h.instances()[0]["provider_ref"] is None
    await h.run_until(t0 + 1000)
    assert h.fake.posts() == 1
    assert h.jobs()[0]["status"] == "completed"
    assert h.incidents("create_unknown_unresolved") == []
    await h.run_until(t0 + 3000)
    assert h.ours() == []


async def test_core_f1_never_visible_fails_without_second_post(tmp_path: Path) -> None:
    h = RunpodCoreHarness(tmp_path, core_cfg(tmp_path))
    await _start(h)
    h.fake.inject("POST", "/pods", "read_timeout")
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 900)
    assert h.fake.posts() == 1
    assert ("PROVISIONING", "ERROR", "create_unknown_unresolved") in h.transitions()
    assert h.incidents("create_unknown_unresolved")[0]["severity"] == "high"
    assert h.jobs()[0]["route"] == "local:fallback"
    await h.run_until(t0 + 4000)
    assert h.fake.posts() == 1 and h.state_of() == "STOPPED"


async def test_core_f1_restart_during_unknown_create(tmp_path: Path) -> None:
    h = RunpodCoreHarness(tmp_path, core_cfg(tmp_path))
    await _start(h)
    h.fake.inject("POST", "/pods", "read_timeout")
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 60)
    assert h.fake.posts() == 1
    h.restart()
    h.clock.advance_to(t0 + 700)
    await _start(h)
    assert ("PROVISIONING", "ERROR", "create_unknown_unresolved") in h.transitions()
    assert h.fake.posts() == 1
    await h.run_until(t0 + 4000)
    # the queued job may start a NEW row (new label); the unknown label is never created twice
    names = [b["name"] for b in h.fake.created_bodies]
    assert len(names) == len(set(names)) and h.ours() == []
