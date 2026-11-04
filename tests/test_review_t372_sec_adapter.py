"""T-372 review cycle 1 (money and security): RunpodAdapter / RunpodApi against the REAL
RunPod openapi (rest.runpod.io/v1, openapi 3.0.3, P0) and money paths not covered elsewhere.

xfail(strict=True) = a confirmed defect (remove the marker with the fix).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from llmhub.clock import FakeClock
from llmhub.provider_adapters.base import DestroyResult, ProviderError
from llmhub.provider_adapters.labels import make_label, new_op_id
from llmhub.provider_adapters.runpod import RunpodAdapter, create_body
from llmhub.provider_adapters.runpod_api import RunpodApi, _Parser, parse_pod
from runpod_fake import GPU_ID, FakeRunpod, make_adapter, make_spec, runpod_cfg
from t372_scripts import load

ROOT = Path(__file__).resolve().parent.parent
OPENAPI = ROOT / "tests" / "fixtures" / "runpod" / "openapi.json"
SECRET = b"test-secret-0123456789abcdef-test-secret"
scan = load("image_secret_scan")


def _real() -> dict[str, Any]:
    if not OPENAPI.exists():
        pytest.skip("P0 openapi.json not downloaded")
    return json.loads(OPENAPI.read_text())


def _label() -> str:
    return make_label(new_op_id(), SECRET)


# ------------------------------------------------------------------ create body vs real schema
def test_create_body_money_fields_against_real_schema() -> None:
    doc = _real()
    props = doc["components"]["schemas"]["PodCreateInput"]["properties"]
    body = create_body(make_spec(), _label(), runpod_cfg(), GPU_ID)
    assert set(body) <= set(props)  # nothing RunPod would ignore silently
    # defaults that would cost money if the field were dropped
    assert props["volumeInGb"]["default"] == 20 and body["volumeInGb"] == 0
    assert props["cloudType"]["default"] == "SECURE" and body["cloudType"] == "COMMUNITY"
    assert body["interruptible"] is False and body["gpuCount"] == 1
    for banned in ("networkVolumeId", "templateId", "containerRegistryAuthId",
                   "globalNetworking", "locked"):  # fmt: skip
        assert banned not in body
    assert set(body["gpuTypeIds"]) <= set(props["gpuTypeIds"]["items"]["enum"])
    assert set(body["allowedCudaVersions"]) <= set(props["allowedCudaVersions"]["items"]["enum"])
    assert 0 < body["containerDiskInGb"] <= 100


def test_parse_pod_real_shape_without_createdat() -> None:
    """The adapter tolerates the missing createdAt and drops env; since fix round 1 (R2-3)
    `created_at` comes from `lastStartedAt`."""
    raw = {"id": "p1", "name": "llmhub-0123456789ab-01234567", "desiredStatus": "RUNNING",
           "costPerHr": 0.22, "adjustedCostPerHr": 0.22, "machineId": "m1",
           "image": "x@sha256:" + "0" * 64, "lastStartedAt": "2026-11-04T10:00:00.000Z",
           "env": {"LLMHUB_API_KEY": "pod-secret"}}  # fmt: skip
    pod = parse_pod(raw, _Parser("GET", "/pods"))
    assert pod.created_at == 1793786400.0 and "pod-secret" not in repr(pod)


async def test_delete_targets_pod_terminate_not_stop() -> None:
    """destroy = DELETE /pods/{id} (terminate); /stop is never called (it keeps billing disk)."""
    doc = _real()
    assert "delete" in doc["paths"]["/pods/{podId}"]
    clock = FakeClock()
    fake = FakeRunpod(clock)
    a = make_adapter(fake)
    res = await a.create(make_spec(), _label())
    assert await a.destroy(res.ref) == DestroyResult.ACCEPTED
    paths = [(r.method, r.url.path) for r in fake.requests]
    assert paths[-1] == ("DELETE", f"/v1/pods/{res.ref}") and not any("/stop" in p for _, p in paths)


# ------------------------------------------------------------------ destroy idempotence
def _adapter(handler: Any) -> RunpodAdapter:
    api = RunpodApi(lambda: "rpa_FAKE" + "k" * 30, transport=httpx.MockTransport(handler))
    return RunpodAdapter(api, runpod_cfg(), FakeClock(), disk_gb_hint=40)


async def test_destroy_gone_pod_answered_400_is_already_gone() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "DELETE":
            return httpx.Response(400, json={"error": "pod not found"})
        if req.method == "GET" and req.url.path.startswith("/v1/pods/"):
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(500)

    assert await _adapter(handler).destroy("gonepod1") == DestroyResult.ALREADY_GONE


async def test_create_single_post_on_read_timeout_no_hidden_retry() -> None:
    """One POST even when the transport times out after sending (no httpx/SDK retry)."""
    posts: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.runpod.io":
            return httpx.Response(200, json={"data": {"gpuTypes": [{
                "id": GPU_ID, "displayName": "RTX 3090", "memoryInGb": 24,
                "lowestPrice": {"uninterruptablePrice": 0.22, "stockStatus": "High"}}]}})
        if req.method == "GET":
            return httpx.Response(200, json=[])
        posts.append(1)
        raise httpx.ReadTimeout("read timeout", request=req)

    with pytest.raises(ProviderError) as ei:
        await _adapter(handler).create(make_spec(), _label())
    assert ei.value.err.outcome_unknown and len(posts) == 1


# ------------------------------------------------------------------ image scan allowlist
BASE = "sha256:5a7813e071bfadf18aaa6ca8318be4824a9b6297b3240f2cc84c1db6f4113040"


def test_allowlist_is_exact_path() -> None:
    """Fix round 1: exact path AND the layer digest (customer condition, REV-S6)."""
    assert scan._allowed("usr/lib/x86_64-linux-gnu/libgnutls.so.30.37.1", "private_key", BASE)
    assert not scan._allowed("usr/lib/x86_64-linux-gnu/libgnutls.so.30.planted", "private_key",
                             BASE)  # fmt: skip
    assert not scan._allowed("usr/lib/x86_64-linux-gnu/libgnutls.so.30.37.1", "private_key",
                             "sha256:" + "0" * 64)  # fmt: skip
    assert not scan._allowed("usr/lib/x86_64-linux-gnu/libgnutls.so.30.37.1", "private_key", None)


def test_allowlist_only_private_key_rule() -> None:
    assert not scan._allowed("usr/lib/x86_64-linux-gnu/libgnutls.so.30.37.1", "github_token", BASE)
    assert not scan._allowed("usr/lib/x86_64-linux-gnu/libgnutls.so.30.37.1", "dotenv", BASE)
