"""In-process fake of the RunPod API (T-372 A5): REST v1 + GraphQL on `httpx.MockTransport`.

Shapes follow the real openapi (`tests/fixtures/runpod/openapi.json`, R2-5): a pod has no
`createdAt`; `desiredStatus` is RUNNING / EXITED / TERMINATED only (RUNNING right after POST);
POST /pods answers 201, DELETE /pods/{id} 204; a deleted pod answers DELETE with
`gone_delete_status` (404 or the documented 400) and GET with 404.
A small state machine on an injected clock:
- pods: desiredStatus RUNNING at once; `lastStartedAt` appears after `boot_s` (image pull);
  `cost_per_hr` = GPU price + disk (`cost_as_string`: the openapi example form "0.74");
- `visible_after_s`: a new pod is missing from `GET /pods` and `GET /pods/{id}` this long;
- `destroy_lag_s`: a deleted pod stays listed as EXITED this long, then 404;
- foreign pods, network volumes and templates can be added;
- faults per request (`inject`): HTTP status with a body, timeouts before/after the effect,
  bad JSON shapes, a broken second page;
- GraphQL `gpuTypes` in the real shape (R3-3): `communityPrice`/`communityCloud`, a null
  `lowestPrice` for `secureCloud: false`;
- billing: hourly buckets per pod from its lifetime;
- capacity per CUDA version (`cuda_capacity`, L2b): a POST whose `allowedCudaVersions` holds a
  version without capacity answers the real 500 `NO_INSTANCES_BODY` (a list of several
  versions narrows the choice, like the real API: every listed version needs capacity).
- several GPU types (`gpus`: id -> Community price, None = `communityCloud` false) and
  capacity per GPU type (`gpu_capacity`): a POST for a type without capacity answers the
  same 500; the pod cost is the price of its type + disk.
The fake answers with the pod `env` (like RunPod), so tests can prove the adapter drops it.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx

from llmhub.clock import FakeClock
from llmhub.configuration.models import RunpodConfig
from llmhub.provider_adapters.base import InstanceSpec
from llmhub.provider_adapters.runpod import RunpodAdapter
from llmhub.provider_adapters.runpod_api import RunpodApi

FAKE_KEY = "rpa_FAKEKEY_0123456789abcdef"  # fictitious
GPU_ID = "NVIDIA GeForce RTX 3090"
IMAGE = "ghcr.io/yvkalabuha/llmhub-infer@sha256:" + "ab" * 32
# the real answer of POST /pods without capacity (L2b, 2026-11-04)
NO_INSTANCES_BODY = ('{"error":"create pod: There are no instances currently available",'
                     '"status":500}')


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class FakePod:
    id: str
    name: str
    image: str
    cost: float
    created_at: float
    env: dict[str, str]
    machine_id: str
    body: dict[str, Any]
    visible_at: float = 0.0
    deleted_at: float | None = None
    exited: bool = False


@dataclass
class Fault:
    method: str
    path: str  # prefix of the path after /v1 ("/pods", "/pods/", "graphql:myself", ...)
    # "status" | "timeout_before" | "read_timeout" | "timeout_after" | "connect" | "shape" |
    # "page2" | "graphql_error"
    kind: str
    status: int = 500
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict[str, str])
    times: int = 1


class FakeRunpod:
    def __init__(
        self,
        clock: FakeClock,
        *,
        price: float = 0.22,
        disk_usd_h: float = 0.005,
        stock: str | None = "High",
        community: bool | None = True,
        secure_price: float = 0.44,
        price_as_string: bool = False,
        balance: float = 25.0,
        boot_s: float = 0.0,
        visible_after_s: float = 0.0,
        destroy_lag_s: float = 0.0,
        cost_override: float | None = None,
        name_override: str | None = None,
        paged: bool = False,
        cost_as_string: bool = False,
        gone_delete_status: int = 404,
        create_answer_extra: dict[str, Any] | None = None,
        has_spend_limit: bool = True,
        cuda_capacity: set[str] | None = None,
        gpus: dict[str, float | None] | None = None,
        gpu_capacity: set[str] | None = None,
        community_stock: str | None | dict[str, str | None] = "High",
    ) -> None:
        self.clock = clock
        self.price = price
        self.disk_usd_h = disk_usd_h
        self.stock = stock  # lowestPrice(gpuCount: 1).stockStatus: a hint only (R3-3)
        self.community = community  # gpuType.communityCloud
        self.secure_price = secure_price  # lowestPrice.uninterruptablePrice may be Secure
        self.price_as_string = price_as_string  # gpuType.communityPrice as "0.22"
        self.balance0 = balance
        self.boot_s = boot_s
        self.visible_after_s = visible_after_s
        self.destroy_lag_s = destroy_lag_s
        self.cost_override = cost_override
        self.name_override = name_override
        self.paged = paged
        self.cost_as_string = cost_as_string
        self.gone_delete_status = gone_delete_status
        self.create_answer_extra = dict(create_answer_extra or {})  # fields of the new pod
        self.has_spend_limit = has_spend_limit  # GraphQL `spendLimit` is unverified (R2-10)
        self.cuda_capacity = cuda_capacity  # None: capacity for every CUDA version
        self.gpus = gpus  # None: only GPU_ID at `price` (and `community`)
        self.gpu_capacity = gpu_capacity  # None: capacity for every GPU type
        # `gpuTypes { id lowestPrice(input: {gpuCount: 1, secureCloud: false}) { stockStatus } }`
        # without an id filter (the stock pre-check, live 2026-11-05): one value or per type
        self.community_stock = community_stock
        self.pods: dict[str, FakePod] = {}
        self.volumes: list[dict[str, Any]] = []
        self.templates: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.faults: list[Fault] = []
        self.created_bodies: list[dict[str, Any]] = []
        self._n = 0
        self.extra_pod_fields: dict[str, Any] = {}

    # --- wiring ----------------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def api(self, key: Callable[[], str] | None = None) -> RunpodApi:
        return RunpodApi(key or (lambda: FAKE_KEY), transport=self.transport())

    # --- test helpers ------------------------------------------------------------
    def inject(self, method: str, path: str, kind: str, **kw: Any) -> None:
        self.faults.append(Fault(method, path, kind, **kw))

    def add_pod(self, name: str, *, status: str = "RUNNING", cost: float = 0.3,
                created_at: float | None = None) -> str:  # fmt: skip
        self._n += 1
        pid = f"forn{self._n:04d}x"
        pod = FakePod(pid, name, "someone/else:latest", cost,
                      self.clock.now() if created_at is None else created_at, {}, f"m-{pid}",
                      {})  # fmt: skip
        self.pods[pid] = pod
        self.extra_pod_fields.setdefault(pid, {})["desiredStatus"] = status
        return pid

    def live_pods(self) -> list[FakePod]:
        """Ground truth: pods that bill (also invisible ones and ones in destroy lag)."""
        now = self.clock.now()
        return [p for p in self.pods.values()
                if p.deleted_at is None or now < p.deleted_at + self.destroy_lag_s]  # fmt: skip

    def posts(self) -> int:
        return sum(1 for r in self.requests if r.method == "POST" and r.url.path == "/v1/pods")

    def billed(self) -> float:
        now = self.clock.now()
        total = 0.0
        for p in self.pods.values():
            end = now if p.deleted_at is None else min(now, p.deleted_at + self.destroy_lag_s)
            total += max(0.0, end - p.created_at) * p.cost / 3600
        return total

    # --- state -------------------------------------------------------------------
    def _status(self, p: FakePod) -> str:
        forced = self.extra_pod_fields.get(p.id, {}).get("desiredStatus")
        if p.deleted_at is not None:
            return "EXITED"
        if forced:
            return str(forced)
        if p.exited:
            return "EXITED"
        return "RUNNING"  # desired, not actual: RUNNING from the POST on (R2-6)

    def _visible(self, p: FakePod) -> bool:
        now = self.clock.now()
        if now < p.visible_at:
            return False
        return p.deleted_at is None or now < p.deleted_at + self.destroy_lag_s

    def _json(self, p: FakePod) -> dict[str, Any]:
        cost: Any = str(p.cost) if self.cost_as_string else p.cost
        d: dict[str, Any] = {
            "id": p.id, "name": p.name, "desiredStatus": self._status(p), "costPerHr": cost,
            "adjustedCostPerHr": cost, "machineId": p.machine_id, "image": p.image,
            "env": dict(p.env), "gpu": {"id": GPU_ID, "count": 1},
            "interruptible": bool(p.body.get("interruptible", False)),
            "ports": list(p.body.get("ports", ["8080/http"])),
            "containerDiskInGb": p.body.get("containerDiskInGb", 20),
            "volumeInGb": p.body.get("volumeInGb", 20), "lastStatusChange": "Rented by User",
        }  # fmt: skip
        if self.clock.now() >= p.created_at + self.boot_s:
            d["lastStartedAt"] = _iso(p.created_at + self.boot_s)
        for k, v in self.extra_pod_fields.get(p.id, {}).items():
            if k != "desiredStatus":
                d[k] = v
        return d

    # --- HTTP ------------------------------------------------------------------------
    def _fault(self, method: str, path: str, *, paged: bool = False) -> Fault | None:
        for f in self.faults:
            if f.kind == "page2" and not paged:
                continue  # only the request for a later page fails
            if f.times > 0 and f.method == method and path.startswith(f.path):
                f.times -= 1
                self.faults = [x for x in self.faults if x.times > 0]
                return f
        return None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {FAKE_KEY}":
            return httpx.Response(401, json={"error": "unauthorized"})
        host = request.url.host
        if host == "api.runpod.io":
            return self._graphql(request)
        assert host == "rest.runpod.io", host
        path = request.url.path.removeprefix("/v1")
        fault = self._fault(request.method, path, paged="cursor=" in str(request.url))
        if fault is not None:
            if fault.kind == "status":
                return httpx.Response(fault.status, text=fault.body, headers=fault.headers)
            if fault.kind == "timeout_before":
                raise httpx.ConnectTimeout("fake", request=request)
            if fault.kind == "read_timeout":  # sent, no effect, no answer
                raise httpx.ReadTimeout("fake", request=request)
            if fault.kind == "connect":
                raise httpx.ConnectError("fake", request=request)
            if fault.kind == "shape":
                return httpx.Response(200, json={"unexpected": True} if path != "/pods"
                                      # our label: strict (a foreign item is skipped, R3-6)
                                      else [{"id": 5, "name": "llmhub-000000000000-00000000"}])  # fmt: skip
        resp = self._route(request, path, fault)
        if fault is not None and fault.kind == "timeout_after":
            raise httpx.ReadTimeout("fake", request=request)
        return resp

    def _route(self, request: httpx.Request, path: str, fault: Fault | None) -> httpx.Response:
        m, q = request.method, parse_qs(request.url.query.decode())
        if path == "/pods" and m == "GET":
            items = [self._json(p) for p in self.pods.values() if self._visible(p)]
            if not self.paged:
                return httpx.Response(200, json=items)
            if "cursor" not in q:
                return httpx.Response(200, json={"data": items[:1], "nextCursor": "c2"})
            if fault is not None and fault.kind == "page2":
                return httpx.Response(503, text="page failed")
            return httpx.Response(200, json={"data": items[1:], "nextCursor": None})
        if path == "/pods" and m == "POST":
            body = json.loads(request.content)
            self.created_bodies.append(body)
            cudas = body.get("allowedCudaVersions")
            if self.cuda_capacity is not None and (
                    not self.cuda_capacity if not cudas
                    else not set(cudas) <= self.cuda_capacity):  # fmt: skip
                return httpx.Response(500, text=NO_INSTANCES_BODY)
            gid = (body.get("gpuTypeIds") or [GPU_ID])[0]
            if self.gpu_capacity is not None and gid not in self.gpu_capacity:
                return httpx.Response(500, text=NO_INSTANCES_BODY)
            self._n += 1
            pid = f"pod{self._n:04d}abc"
            now = self.clock.now()
            gpu_price = self.gpu_price(gid)
            cost = self.cost_override if self.cost_override is not None else round(
                (gpu_price if gpu_price is not None else self.price) + self.disk_usd_h, 4)
            pod = FakePod(pid, self.name_override or body["name"], body["imageName"], cost, now,
                          dict(body.get("env", {})), f"mach-{self._n}", body,
                          visible_at=now + self.visible_after_s)  # fmt: skip
            self.pods[pid] = pod
            if self.create_answer_extra:
                self.extra_pod_fields.setdefault(pid, {}).update(self.create_answer_extra)
            return httpx.Response(201, json=self._json(pod))
        mm = re.fullmatch(r"/pods/([^/]+)", path)
        if mm and m in ("GET", "DELETE"):
            pod = self.pods.get(mm.group(1))
            if pod is None or not self._visible(pod):
                if m == "DELETE" and pod is not None and pod.deleted_at is None:
                    pod.deleted_at = self.clock.now()  # invisible yet, still deletable
                    return httpx.Response(204)
                if m == "DELETE":
                    return httpx.Response(self.gone_delete_status, json={"error": "pod not found"})
                return httpx.Response(404, json={"error": "pod not found"})
            if m == "GET":
                return httpx.Response(200, json=self._json(pod))
            if pod.deleted_at is None:
                pod.deleted_at = self.clock.now()
            return httpx.Response(204)
        if path == "/networkvolumes" and m == "GET":
            return httpx.Response(200, json=self.volumes)
        if path == "/templates" and m == "GET":
            return httpx.Response(200, json=self.templates)
        if path == "/billing/pods" and m == "GET":
            return httpx.Response(200, json=self._billing(q))
        return httpx.Response(404, json={"error": "no route"})

    def _billing(self, q: dict[str, list[str]]) -> list[dict[str, Any]]:
        from llmhub.provider_adapters.runpod_api import parse_time

        start = parse_time(q["startTime"][0])
        end = parse_time(q["endTime"][0])
        out: list[dict[str, Any]] = []
        for p in self.pods.values():
            stop = self.clock.now() if p.deleted_at is None else p.deleted_at + self.destroy_lag_s
            t0, t1 = max(p.created_at, start), min(stop, end)
            if t1 <= t0:
                continue
            hour = int(t0 // 3600) * 3600
            while hour < t1:
                a, b = max(t0, hour), min(t1, hour + 3600)
                if b > a:
                    out.append({"podId": p.id, "time": _iso(hour),
                                "amount": round((b - a) * p.cost / 3600, 6),
                                "diskSpaceBilledGb": 0})  # fmt: skip
                hour += 3600
        return out

    def _graphql(self, request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        if "myself" in query:
            fault = self._fault("POST", "graphql:myself")
            if fault is not None:
                return httpx.Response(fault.status, text=fault.body)
            if "spendLimit" in query and not self.has_spend_limit:
                return httpx.Response(200, json={"errors": [{"message": 'Cannot query field'
                                                             ' "spendLimit" on type "User".'}]})  # fmt: skip
            spend = sum(p.cost for p in self.pods.values() if self._status(p) == "RUNNING"
                        and p.deleted_at is None)  # fmt: skip
            return httpx.Response(200, json={"data": {"myself": {
                "clientBalance": round(self.balance0 - self.billed(), 6),
                "currentSpendPerHr": spend,
                **({"spendLimit": 80} if self.has_spend_limit else {})}}})  # fmt: skip
        if "gpuTypes" in query:
            fault = self._fault("POST", "graphql:gpuTypes")
            if fault is not None:
                if fault.kind == "graphql_error":
                    return httpx.Response(200, json={"errors": [{"message": fault.body}],
                                                     "data": None})  # fmt: skip
                return httpx.Response(fault.status, text=fault.body)
            gid = re.search(r'id: "([^"]+)"', query)
            known = self.gpus if self.gpus is not None else {GPU_ID: self.price}
            if gid is None:  # the stock query: every type, Community stockStatus only
                return httpx.Response(200, json={"data": {"gpuTypes": [
                    {"id": k, "lowestPrice": {"stockStatus": self.stock_of(k)}} for k in known
                ]}})  # fmt: skip
            if gid.group(1) not in known:
                return httpx.Response(200, json={"data": {"gpuTypes": []}})
            item = self._gpu_type(query, gid.group(1))
            return httpx.Response(200, json={"data": {"gpuTypes": [item]}})
        return httpx.Response(400, json={"errors": [{"message": "unknown query"}]})

    def stock_of(self, gid: str) -> str | None:
        if isinstance(self.community_stock, dict):
            return self.community_stock.get(gid)
        return self.community_stock

    def gpu_price(self, gid: str) -> float | None:
        if self.gpus is None:
            return self.price if gid == GPU_ID else None
        return self.gpus.get(gid)

    def _gpu_type(self, query: str, gid: str = GPU_ID) -> dict[str, Any]:
        """The real answer shape (L1, 2026-11-04): `communityPrice`/`communityCloud` on the
        type; `lowestPrice(input: {gpuCount: 1, secureCloud: false})` -> every field null;
        without `secureCloud` -> a price (possibly Secure) and `stockStatus`. Only the
        selected fields are returned, like GraphQL."""
        own = self.gpu_price(gid)
        community = self.community if self.gpus is None else own is not None
        raw = own if own is not None else self.price
        price: Any = str(raw) if self.price_as_string else raw
        full: dict[str, Any] = {"id": gid, "displayName": gid.removeprefix("NVIDIA GeForce "),
                                "memoryInGb": 24,
                                "communityPrice": price, "communityCloud": community,
                                "secureCloud": True, "securePrice": self.secure_price}  # fmt: skip
        item = {k: v for k, v in full.items() if re.search(rf"\b{k}\b", query)}
        m = re.search(r"lowestPrice\(input: *\{([^}]*)\}\)\s*\{([^}]*)\}", query)
        if m is not None:
            null = "secureCloud" in m.group(1)
            low = {"uninterruptablePrice": None if null else self.secure_price,
                   "minimumBidPrice": None,
                   "stockStatus": None if null else self.stock}  # fmt: skip
            item["lowestPrice"] = {k: v for k, v in low.items() if k in m.group(2).split()}
        return item


def runpod_cfg(**over: Any) -> RunpodConfig:
    return RunpodConfig(**over)


def make_spec(**over: Any) -> InstanceSpec:
    fields: dict[str, Any] = {
        "pool": "cloud-27b", "offer_id": GPU_ID, "image": IMAGE, "disk_gb": 40,
        "max_price_usd_h": 0.30, "max_lifetime_s": 3600,
        "env": {"LLMHUB_TRANSPORT": "proxy", "LLMHUB_PORT": "8080", "LLMHUB_OP_ID": "x"},
    }  # fmt: skip
    fields.update(over)
    return InstanceSpec(**fields)


def make_adapter(fake: FakeRunpod, **cfg_over: Any) -> RunpodAdapter:
    return RunpodAdapter(fake.api(), runpod_cfg(**cfg_over), fake.clock, disk_gb_hint=40)


class RunpodHarness:
    """AdapterHarness (contract C1..C11) for RunpodAdapter on the fake."""

    def __init__(self, *, lagging: bool = False) -> None:
        self.clock = FakeClock()
        self.fake = FakeRunpod(
            self.clock, destroy_lag_s=30.0 if lagging else 0.0,
            visible_after_s=0.0, boot_s=20.0 if lagging else 0.0,
        )  # fmt: skip
        self.adapter = make_adapter(self.fake)
        self.lagging = lagging

    async def settle(self) -> None:
        self.clock.advance_to(self.clock.now() + 120.0)

    def make_spec(self) -> InstanceSpec:
        return make_spec()

    def add_foreign(self, label: str) -> str:
        return self.fake.add_pod(label)

    def inject_unknown_create(self) -> None:
        self.fake.inject("POST", "/pods", "timeout_after")
