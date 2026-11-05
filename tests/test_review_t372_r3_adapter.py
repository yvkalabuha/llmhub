"""T-372 review cycle 3: RunpodAdapter against the REAL GraphQL answers of the L1 read-only
check (coordinator, 2026-11-04; raw answers kept out of git, values copied below).

Real behaviour: `lowestPrice(input: {gpuCount: 1, secureCloud: false})` -> every field null for
every GPU type; `lowestPrice(input: {gpuCount: 1})` -> 3090 uninterruptablePrice 0.22,
stockStatus "Low"; `communityPrice` 0.22 and `communityCloud` true on the type itself;
`myself` has `spendLimit` (80).

The confirmed defect (R3-3) is fixed in fix round 2; markers removed.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest

from llmhub.clock import FakeClock
from llmhub.provider_adapters.base import OfferQuery, ProviderError
from llmhub.provider_adapters.labels import make_label, new_op_id
from llmhub.provider_adapters.runpod import RunpodAdapter
from llmhub.provider_adapters.runpod_api import RunpodApi
from runpod_fake import FAKE_KEY, GPU_ID, make_spec, runpod_cfg

SECRET = b"test-secret-0123456789abcdef-test-secret"


def _real_graphql(query: str) -> dict[str, Any]:
    if "myself" in query:
        return {"data": {"myself": {"clientBalance": 19.8420546925, "currentSpendPerHr": 0,
                                    "spendLimit": 80}}}  # fmt: skip
    gid = re.search(r'id: "([^"]+)"', query)
    if gid is None:  # the stock pre-check (all types): L1 answered null for every type
        return {"data": {"gpuTypes": [{"id": GPU_ID, "lowestPrice": {"stockStatus": None}}]}}
    low: dict[str, Any] = {}
    m = re.search(r"lowestPrice\(input: *\{([^}]*)\}\)", query)
    if m is not None:
        null = "secureCloud" in m.group(1)  # the L1 finding: secureCloud:false -> all null
        low = {"uninterruptablePrice": None if null else 0.22,
               "minimumBidPrice": None, "stockStatus": None if null else "Low"}  # fmt: skip
    item: dict[str, Any] = {"id": gid.group(1), "displayName": "RTX 3090", "memoryInGb": 24,
                            "communityPrice": 0.22, "communityCloud": True,
                            "secureCloud": True}  # fmt: skip
    if m is not None:
        item["lowestPrice"] = low
    return {"data": {"gpuTypes": [item] if gid.group(1) == GPU_ID else []}}


def _adapter(seen: list[str]) -> RunpodAdapter:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.runpod.io":
            q = json.loads(request.content)["query"]
            seen.append(q)
            return httpx.Response(200, json=_real_graphql(q))
        if request.method == "GET" and request.url.path.endswith("/pods"):
            return httpx.Response(200, json=[])
        return httpx.Response(599, json={})  # no POST in these tests

    api = RunpodApi(lambda: FAKE_KEY, transport=httpx.MockTransport(handle))
    return RunpodAdapter(api, runpod_cfg(), FakeClock(), disk_gb_hint=40)


async def test_offer_found_on_real_graphql_answer() -> None:
    seen: list[str] = []
    offers = await _adapter(seen).list_offers(
        OfferQuery(gpu="RTX3090", min_vram_gb=24, max_price_usd_h=0.30, exclude_hosts=frozenset()))
    assert [o.price_usd_h for o in offers] == [0.22]


async def test_create_precheck_passes_on_real_graphql_answer() -> None:
    seen: list[str] = []
    with pytest.raises(ProviderError) as e:
        await _adapter(seen).create(make_spec(), make_label(new_op_id(), SECRET))
    # past the price check, the POST (599 here) is reached
    assert "no community offer" not in e.value.err.message


async def test_no_community_offer_means_no_post() -> None:
    """Safe side of R3-3: `communityCloud` false -> refused before any POST."""
    posts: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.host != "api.runpod.io":
            posts.append(str(request.url))
        if request.url.host == "api.runpod.io":
            doc = _real_graphql(json.loads(request.content)["query"])
            for item in doc["data"].get("gpuTypes", []):
                item["communityCloud"] = False
            return httpx.Response(200, json=doc)
        return httpx.Response(200, json=[])

    api = RunpodApi(lambda: FAKE_KEY, transport=httpx.MockTransport(handle))
    ad = RunpodAdapter(api, runpod_cfg(), FakeClock(), disk_gb_hint=40)
    with pytest.raises(ProviderError):
        await ad.create(make_spec(), make_label(new_op_id(), SECRET))
    assert posts == []


async def test_myself_real_answer_with_spend_limit() -> None:
    seen: list[str] = []
    me = await _adapter(seen).api.myself()
    assert (me.balance_usd, me.spend_per_hr, me.spend_limit) == (19.8420546925, 0.0, 80.0)
    assert len(seen) == 1  # no fallback query needed
