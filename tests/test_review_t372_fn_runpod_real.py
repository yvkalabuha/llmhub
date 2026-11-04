"""T-372 review cycle 2 (correctness against the REAL RunPod API).

Ground truth: `tests/fixtures/runpod/openapi.json` (openapi 3.0.3, downloaded by the coordinator
from https://rest.runpod.io/v1/openapi.json). Confirmed defects are `xfail(strict=True)`: they
turn into failures once fixed, so the marker has to be removed together with the fix.

The real `Pod` schema has NO `createdAt` (it has `lastStartedAt`, `lastStatusChange`), its
`desiredStatus` enum is RUNNING / EXITED / TERMINATED only, POST /pods answers 201, DELETE
/pods/{id} answers 204 (no 404 documented), `allowedCudaVersions` also allows "13.0".
"""

from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path
from typing import Any, Self

import httpx
import pytest

from llmhub.clock import FakeClock
from llmhub.configuration.loader import load_config
from llmhub.configuration.models import RunpodConfig
from llmhub.persistence.db import connect, migrate
from llmhub.provider_adapters.base import ProviderError
from llmhub.provider_adapters.runpod import RunpodAdapter
from llmhub.provider_adapters.runpod_api import _Parser, parse_pod
from llmhub.tools.t372_ops import lifecycle_probe
from runpod_fake import GPU_ID, FakeRunpod, make_spec, runpod_cfg
from t372_scripts import load

g = load("runpod_guard")

REAL = Path(__file__).parent / "fixtures" / "runpod" / "openapi.json"
needs_real = pytest.mark.skipif(not REAL.exists(), reason="P0: real openapi.json not present")

T = 1_793_750_400.0
OURS = "llmhub-0123456789ab-01234567"
RO = "rpa_READONLY" + "r" * 30  # fictitious
SMALL = "nvidia/cuda@sha256:" + "c" * 64


def _iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def real_pod(pid: str = "rp0001abcd", name: str = OURS, *, age_s: float = 60) -> dict[str, Any]:
    """A pod shaped like the openapi `Pod` schema (no `createdAt`)."""
    return {
        "id": pid, "name": name, "desiredStatus": "RUNNING", "costPerHr": 0.22,
        "adjustedCostPerHr": 0.22, "machineId": "m1x2y3", "image": SMALL,
        "lastStartedAt": _iso(T - age_s), "lastStatusChange": "Rented by User: ...",
        "containerDiskInGb": 20, "volumeInGb": 0, "gpu": {"id": GPU_ID, "count": 1},
        "interruptible": False, "ports": ["8080/http"], "publicIp": None, "portMappings": None,
        "env": {"LLMHUB_API_KEY": "SYNTHETIC"},
    }  # fmt: skip


def _schemas() -> dict[str, Any]:
    return json.loads(REAL.read_text())["components"]["schemas"]


# ------------------------------------------------------------------ the shape helper itself
@needs_real
def test_real_pod_helper_matches_openapi() -> None:
    props = set(_schemas()["Pod"]["properties"])
    assert set(real_pod()) <= props
    assert "createdAt" not in props  # the coordinator's finding


# ------------------------------------------------------------------ guard (stdlib, raw JSON)
class _Resp(io.BytesIO):
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *a: object) -> None:
        self.close()


class _Opener:
    def __init__(self, pods: list[dict[str, Any]]) -> None:
        self.pods = pods

    def open(self, req: Any, timeout: float = 0) -> _Resp:
        url = req.full_url
        if url == g.GRAPHQL:
            return _Resp(json.dumps({"data": {"myself": {
                "clientBalance": 20.0, "currentSpendPerHr": 0.22}}}).encode())  # fmt: skip
        path = url.split("?", 1)[0].replace(g.REST, "")
        return _Resp(json.dumps(self.pods if path == "/pods" else []).encode())


def test_guard_snapshot_accepts_real_pod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(g, "OPENER", _Opener([real_pod()]))
    monkeypatch.setattr(g, "NOW", lambda: T)
    key = tmp_path / "ro"
    key.write_text(RO + "\n")
    key.chmod(0o600)
    assert g.main(["snapshot", "--key-file", str(key), "--out", str(tmp_path / "s.json")]) == 0


def test_guard_age_trigger_works_without_created_at() -> None:
    before = {"pods": [], "volumes": [], "templates": [], "registry_auths": [], "endpoints": []}
    pods = g.parse_pods([real_pod(age_s=80 * 60)])
    now = {**before, "pods": pods, "spend_per_hr": 0.22}
    assert "age>75min" in g.triggers(before, now, T)


# ------------------------------------------------------------------ adapter parsing
def test_parse_pod_takes_a_time_from_the_real_schema() -> None:
    pod = parse_pod(real_pod(), _Parser("GET", "/pods"))
    assert pod.created_at is not None


def test_parse_pod_real_shape_drops_env_and_keeps_fields() -> None:
    pod = parse_pod(real_pod(), _Parser("GET", "/pods"))
    assert (pod.id, pod.name, pod.desired_status, pod.cost_per_hr, pod.machine_id, pod.image) == (
        "rp0001abcd", OURS, "RUNNING", 0.22, "m1x2y3", SMALL)
    assert "SYNTHETIC" not in repr(pod)


# ------------------------------------------------------------------ create body vs PodCreateInput
@needs_real
def test_create_body_matches_pod_create_input_types_and_enums() -> None:
    from llmhub.provider_adapters.runpod import create_body

    s = _schemas()["PodCreateInput"]["properties"]
    body = create_body(make_spec(), OURS, RunpodConfig(), GPU_ID, docker_start_cmd=["sleep", "1"])
    forbidden = {"networkVolumeId", "templateId", "containerRegistryAuthId"}
    assert not forbidden & set(body)
    types = {"string": str, "integer": int, "boolean": bool, "array": list, "object": dict}
    for k, v in body.items():
        assert isinstance(v, types[s[k]["type"]]), k
        enum = s[k].get("enum") or (s[k].get("items") or {}).get("enum")
        if enum:
            assert set(v if isinstance(v, list) else [v]) <= set(enum), k
    assert all(isinstance(x, str) for x in body["env"].values())
    assert body["ports"] == ["8080/http"]  # "[port]/[http|tcp]"
    assert len(body["name"]) <= s["name"]["maxLength"]


@needs_real
def test_allowed_cuda_versions_cover_every_real_version_from_12_8() -> None:
    enum = _schemas()["PodCreateInput"]["properties"]["allowedCudaVersions"]["items"]["enum"]
    want = {v for v in enum if tuple(map(int, v.split("."))) >= (12, 8)}
    assert want <= set(RunpodConfig().allowed_cuda_versions)


# ------------------------------------------------------------------ fake RunPod (A5) fidelity
@needs_real
async def test_fake_pod_json_is_real_shaped() -> None:
    from llmhub.provider_adapters.runpod import create_body

    s = _schemas()["Pod"]["properties"]
    fake = FakeRunpod(FakeClock(), boot_s=30)
    resp = await _post(fake, create_body(make_spec(), OURS, RunpodConfig(), GPU_ID))
    assert resp.status_code == 201
    pod = resp.json()
    assert set(pod) <= set(s), set(pod) - set(s)
    assert pod["desiredStatus"] in s["desiredStatus"]["enum"]


async def _post(fake: FakeRunpod, body: dict[str, Any]) -> httpx.Response:
    async with httpx.AsyncClient(transport=fake.transport()) as c:
        return await c.post("https://rest.runpod.io/v1/pods", json=body,
                            headers={"Authorization": "Bearer rpa_FAKEKEY_0123456789abcdef"})  # fmt: skip


# ------------------------------------------------------------------ lifecycle-probe
class _AutoClock(FakeClock):
    async def sleep_until(self, t: float) -> None:
        if t > self.now():
            self.advance_to(t)


def _cfg(tmp: Path) -> Any:
    import yaml

    from runpod_fake import IMAGE

    raw = {
        "data_dir": str(tmp / "data"),
        "pools": {"cloud-27b": {
            "provider": "runpod", "gpu": "RTX3090", "vram_gb": 24, "pp_tps": 1200,
            "tg_tps": 78, "image": IMAGE, "disk_gb": 40, "max_price_usd_h": 0.30,
            "models": ["m1"]}},
        "providers": {"enabled": ["runpod"], "runpod": {"list_staleness_s": 0}},
        "timeouts": {"create_visibility_grace_s": 600, "offer_wait_s": 300,
                     "provisioning_s": 960},
        "retry": {"create_unknown_max_retries": 0},
    }  # fmt: skip
    (tmp / "data").mkdir(exist_ok=True)
    p = tmp / "config.yaml"
    p.write_text(yaml.safe_dump(raw))
    return load_config(p)


async def test_probe_destroys_known_ref_even_if_listing_fails(tmp_path: Path) -> None:
    clock = _AutoClock()
    fake = FakeRunpod(clock)
    fake.inject("GET", "/networkvolumes", "status", status=503, times=10_000)
    adapter = RunpodAdapter(fake.api(), runpod_cfg(list_staleness_s=0), clock, disk_gb_hint=20)
    conn = connect(tmp_path / "s.db")
    migrate(conn)
    with pytest.raises(ProviderError):
        await lifecycle_probe(conn, adapter, b"x" * 32, clock, _cfg(tmp_path), image=SMALL,
                              gpu_type_id=GPU_ID, max_s=60, poll_s=5)  # fmt: skip
    assert fake.live_pods() == []


async def test_probe_pod_lives_long_enough_for_the_guard(tmp_path: Path) -> None:
    clock = _AutoClock()
    fake = FakeRunpod(clock, boot_s=0)  # real semantics: desiredStatus RUNNING at once
    adapter = RunpodAdapter(fake.api(), runpod_cfg(list_staleness_s=0), clock, disk_gb_hint=20)
    conn = connect(tmp_path / "s.db")
    migrate(conn)
    rep = await lifecycle_probe(conn, adapter, b"x" * 32, clock, _cfg(tmp_path), image=SMALL,
                                gpu_type_id=GPU_ID, max_s=300, poll_s=5)  # fmt: skip
    pod = next(iter(fake.pods.values()))
    assert pod.deleted_at is not None and pod.deleted_at - pod.created_at >= 120
    assert rep.zero
