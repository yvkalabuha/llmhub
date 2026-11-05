"""Thin RunPod API client (T-372 plan 3.1/3.3): REST v1 + GraphQL over httpx, no `runpod` SDK.

Rules:
- no I/O at import or construction (contract C10); the HTTP client is created on first use;
- the API key is sent only as `Authorization: Bearer` (never `?api_key=`), read lazily through
  the injected callable and never kept in an attribute, an error or a log line;
- errors carry the method, the PATH TEMPLATE, the status and a short derived code; never the
  response body, the request body or the key;
- a response of an unexpected shape raises `RunpodParseError` (a stop condition of the live
  test, critic F2): nothing is guessed, a list is never partial;
- the pod `env` (it holds the pod secrets) is never parsed, kept or repr'ed (C11).
Field names follow the public `openapi.json` of `rest.runpod.io/v1` (checked by
`tests/test_t372_runpod_schema.py` against the downloaded file, P0).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from llmhub.provider_adapters.rate_limit import RATE_LIMITED, RestLimiter

REST_BASE = "https://rest.runpod.io/v1"
GRAPHQL_URL = "https://api.runpod.io/graphql"
MAX_PAGES = 50
_BODY_SCAN = 4096

OUR_PREFIX = "llmhub-"  # pods with this name prefix are parsed strictly (R3-6)
_log = logging.getLogger("llmhub.runpod")

# httpx logs request URLs at INFO; keep it quiet (no ids or hosts in the service log)
logging.getLogger("httpx").setLevel(logging.WARNING)


class RunpodHttpError(Exception):
    """A failed RunPod call. `str()` = "<METHOD> <path template> -> <status> <code>"."""

    def __init__(
        self,
        *,
        method: str,
        path: str,
        status: int | None,
        code: str | None,
        retry_after_s: float | None,
        sent: bool,
    ) -> None:
        self.method = method
        self.path = path
        self.status = status
        self.code = code
        self.retry_after_s = retry_after_s
        self.sent = sent  # the request may have reached RunPod (create: unknown outcome)
        super().__init__(f"{method} {path} -> {status if status is not None else '-'} {code or ''}"
                         .rstrip())  # fmt: skip


class RunpodParseError(RunpodHttpError):
    """The answer had an unexpected shape (missing field, wrong type)."""


@dataclass(frozen=True)
class PodInfo:
    id: str
    name: str
    desired_status: str
    cost_per_hr: float | None
    machine_id: str | None
    image: str | None
    created_at: float | None  # `lastStartedAt` (the real Pod has no createdAt, R2-3)
    # money fields of the answer, checked after create (REV-S10); None = not in the answer
    volume_in_gb: float | None = None
    interruptible: bool | None = None
    network_volume_id: str | None = None
    # those of the three above with an unexpected type: never a parse error (a foreign pod
    # must not blind `list_pods`), but a mismatch for a pod we just created
    odd_fields: tuple[str, ...] = ()  # env is never kept (C11)


@dataclass(frozen=True)
class NamedRef:
    id: str
    name: str


@dataclass(frozen=True)
class GpuTypeInfo:
    id: str
    display_name: str
    memory_gb: int
    # `communityPrice` when `communityCloud` is true (R3-3), else None: the Community offer
    community_price: float | None
    stock_status: str | None  # `lowestPrice(input: {gpuCount: 1}).stockStatus`: a hint only


@dataclass(frozen=True)
class AccountInfo:
    balance_usd: float
    spend_per_hr: float
    spend_limit: float | None


@dataclass(frozen=True)
class BillingBucket:
    pod_id: str | None
    start: float
    end: float
    amount_usd: float


# ------------------------------------------------------------------ parsing helpers
_NO_INSTANCES = "no instances currently available"


def _json_error(text: str) -> str | None:
    """The `error` string of a JSON error body, or None."""
    try:
        raw = json.loads(text[:_BODY_SCAN])
    except ValueError:
        return None
    err = raw.get("error") if isinstance(raw, dict) else None
    return err if isinstance(err, str) else None


def classify(text: str, *, status: int | None = None) -> str | None:
    """Short error code from a response text (the text itself is never kept).
    L2b: the real `POST /pods` without capacity answers 500 with JSON
    `{"error": "create pod: There are no instances currently available", ...}`: a definite
    refusal (`no_capacity`); only that exact pair, any other 5xx stays an unknown outcome.
    A 429 (`{"error": "rate limit exceeded for the hour window", "status": 429}`, live
    2026-11-05) is `rate_limited`: the gateway refused the request before it was processed."""
    if status == 429:
        return RATE_LIMITED
    if status == 500:
        err = _json_error(text)
        if err is not None and _NO_INSTANCES in err.lower():
            return "no_capacity"
    t = text[:_BODY_SCAN].lower()
    if "no longer any instances available" in t or "no instances available" in t:
        return "no_capacity"
    if "not enough" in t and "available" in t:
        return "no_capacity"
    if "spend limit" in t or "spending limit" in t:
        return "spend_limit"
    if "insufficient" in t or "balance" in t:
        return "insufficient_balance"
    return None


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


class _Parser:
    """Field access that raises RunpodParseError with the call's method/path."""

    def __init__(self, method: str, path: str) -> None:
        self.method = method
        self.path = path

    def fail(self, what: str) -> RunpodParseError:
        return RunpodParseError(method=self.method, path=self.path, status=None,
                                code=f"parse:{what}"[:80], retry_after_s=None, sent=True)  # fmt: skip

    def obj(self, v: Any, what: str) -> Mapping[str, Any]:
        if not isinstance(v, dict):
            raise self.fail(what)
        return v

    def arr(self, v: Any, what: str) -> list[Any]:
        if not isinstance(v, list):
            raise self.fail(what)
        return v

    def string(self, d: Mapping[str, Any], key: str, *, optional: bool = False) -> str | None:
        v = d.get(key)
        if v is None and optional:
            return None
        if not isinstance(v, str):
            raise self.fail(key)
        return v

    def req_str(self, d: Mapping[str, Any], key: str) -> str:
        v = self.string(d, key)
        assert v is not None
        return v

    def number(self, d: Mapping[str, Any], key: str, *, optional: bool = False) -> float | None:
        v = d.get(key)
        if v is None and optional:
            return None
        if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
            raise self.fail(key)
        return float(v)

    def money(self, d: Mapping[str, Any], key: str) -> float | None:
        """Optional number; a numeric string is accepted (openapi `costPerHr`: type number,
        format currency, example "0.74"; REV-S5/R2-9)."""
        v = d.get(key)
        if isinstance(v, str):
            try:
                f = float(v.strip())
            except ValueError:
                raise self.fail(key) from None
            if not math.isfinite(f):
                raise self.fail(key)
            return f
        return self.number(d, key, optional=True)

    def req_num(self, d: Mapping[str, Any], key: str) -> float:
        v = self.number(d, key)
        assert v is not None
        return v

    def timestamp(self, d: Mapping[str, Any], key: str, *, optional: bool = True) -> float | None:
        v = d.get(key)
        if v is None and optional:
            return None
        if isinstance(v, bool):
            raise self.fail(key)
        if isinstance(v, int | float):
            return float(v) / 1000.0 if v > 1e11 else float(v)  # epoch ms or s
        if isinstance(v, str):
            try:
                return parse_time(v)
            except ValueError:
                raise self.fail(key) from None
        raise self.fail(key)


def parse_time(text: str) -> float:
    """ISO-8601 (`Z`, offsets, RunPod's `2026-11-04 10:00:00.123 +0000 UTC`) -> epoch s."""
    s = text.strip()
    if s.endswith(" UTC"):
        s = s[:-4].strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    for fmt in (None, "%Y-%m-%d %H:%M:%S.%f %z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            t = dt.datetime.fromisoformat(s) if fmt is None else dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.UTC)
        return t.timestamp()
    raise ValueError("bad time")


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_pod(raw: Any, p: _Parser) -> PodInfo:
    d = p.obj(raw, "pod")
    pod_id = p.req_str(d, "id")
    if not pod_id:
        raise p.fail("id")
    image = p.string(d, "image", optional=True)
    if image is None:
        image = p.string(d, "imageName", optional=True)
    started = d.get("lastStartedAt")
    odd: list[str] = []
    vol = d.get("volumeInGb")
    if vol is not None and (isinstance(vol, bool) or not isinstance(vol, int | float)):
        odd.append("volumeInGb")
        vol = None
    flag = d.get("interruptible")
    if flag is not None and not isinstance(flag, bool):
        odd.append("interruptible")
        flag = None
    nv = d.get("networkVolume")
    nv_id: str | None = None
    if isinstance(nv, dict):  # an object with an id = a network volume is attached
        raw_id = nv.get("id")
        nv_id = raw_id if isinstance(raw_id, str) and raw_id else None
        if raw_id is not None and not isinstance(raw_id, str):
            odd.append("networkVolume")
    elif nv is not None:
        odd.append("networkVolume")
    return PodInfo(
        id=pod_id,
        name=p.req_str(d, "name"),
        desired_status=p.req_str(d, "desiredStatus"),
        cost_per_hr=p.money(d, "costPerHr"),
        machine_id=p.string(d, "machineId", optional=True),
        image=image,
        created_at=None if started in (None, "") else p.timestamp(d, "lastStartedAt"),
        volume_in_gb=None if vol is None else float(vol),
        interruptible=flag,
        network_volume_id=nv_id,
        odd_fields=tuple(odd),
    )


def _named(raw: Any, p: _Parser) -> NamedRef:
    d = p.obj(raw, "item")
    return NamedRef(id=p.req_str(d, "id"), name=p.req_str(d, "name"))


# R3-3: the real API answers `lowestPrice(input: {gpuCount: 1, secureCloud: false})` with nulls
# for every type (L1, 2026-11-04); the Community price is `communityPrice` + `communityCloud` of
# the type itself. `lowestPrice(input: {gpuCount: 1})` may quote a Secure price, so only its
# `stockStatus` is read (a hint: a null never drops the offer; no capacity -> POST refused).
_GPU_FIELDS = (
    "id displayName memoryInGb communityPrice communityCloud"
    " lowestPrice(input: {gpuCount: 1}) { stockStatus }"
)


class UnlistedPodError(RunpodHttpError):
    """GET/DELETE /pods/{id} -> 400 and the pod is not in `GET /pods`, but the caller asked
    not to trust an unlisted pod as gone (it is too young for the list, R3-5)."""


class RunpodApi:
    def __init__(
        self,
        key: Callable[[], str],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 20.0,
        rest_base: str = REST_BASE,
        graphql_url: str = GRAPHQL_URL,
        limiter: RestLimiter | None = None,
    ) -> None:
        self._key = key
        self.limiter = limiter  # REST only (GraphQL has its own, wider limit)
        self._transport = transport
        self._timeout_s = timeout_s
        self.rest_base = rest_base.rstrip("/")
        self.graphql_url = graphql_url
        self._client: httpx.AsyncClient | None = None

    def __repr__(self) -> str:
        return f"RunpodApi(rest_base={self.rest_base!r})"

    # --- transport ---------------------------------------------------------
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout_s, follow_redirects=False,
                trust_env=False,
            )  # fmt: skip
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self, method: str, path: str) -> dict[str, str]:
        try:
            key = self._key()
        except Exception:  # noqa: BLE001 - never show why (could quote the file)
            raise RunpodHttpError(method=method, path=path, status=None, code="key_unavailable",
                                  retry_after_s=None, sent=False) from None  # fmt: skip
        return {"Authorization": f"Bearer {key}", "Accept": "application/json"}

    async def _send(
        self,
        method: str,
        url: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
        allow_404: bool = False,
    ) -> Any:
        """One request -> decoded JSON; `None` for an allowed 404."""
        rest = self.limiter is not None and url.startswith(self.rest_base)
        if rest:
            assert self.limiter is not None
            refused = self.limiter.admit(method, path)
            if refused is not None:  # nothing is sent: a definite refusal
                raise RunpodHttpError(method=method, path=path, status=None, code=refused,
                                      retry_after_s=self.limiter.wait_s(refused),
                                      sent=False)  # fmt: skip
        headers = self._headers(method, path)
        try:
            resp = await self._http().request(
                method, url, params=dict(params) if params else None, json=body, headers=headers
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise RunpodHttpError(method=method, path=path, status=None, code="connect",
                                  retry_after_s=None, sent=False) from None  # fmt: skip
        except httpx.TimeoutException:
            raise RunpodHttpError(method=method, path=path, status=None, code="timeout",
                                  retry_after_s=None, sent=True) from None  # fmt: skip
        except httpx.HTTPError:
            raise RunpodHttpError(method=method, path=path, status=None, code="transport",
                                  retry_after_s=None, sent=True) from None  # fmt: skip
        retry_after = _retry_after(resp)
        if rest:
            assert self.limiter is not None
            held = self.limiter.on_response(method, resp.status_code, retry_after)
            if held is not None:
                retry_after = held
        if resp.status_code == 404 and allow_404:
            return None
        if resp.status_code >= 300:
            st = resp.status_code
            raise RunpodHttpError(
                method=method, path=path, status=st, code=classify(resp.text, status=st),
                retry_after_s=retry_after, sent=True,
            )  # fmt: skip
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            raise _Parser(method, path).fail("json") from None

    async def _rest(self, method: str, path: str, template: str, **kw: Any) -> Any:
        return await self._send(method, self.rest_base + path, template, **kw)

    async def _graphql(self, query: str, template: str) -> Mapping[str, Any]:
        raw = await self._send("POST", self.graphql_url, template, body={"query": query})
        p = _Parser("POST", template)
        doc = p.obj(raw, "graphql")
        errors = doc.get("errors")
        if errors:
            text = json.dumps(errors)[:_BODY_SCAN]
            raise RunpodHttpError(method="POST", path=template, status=200,
                                  code=classify(text) or "graphql_error", retry_after_s=None,
                                  sent=True)  # fmt: skip
        return p.obj(doc.get("data"), "data")

    async def _list(self, path: str, template: str, params: Mapping[str, str] | None = None
                    ) -> list[Any]:  # fmt: skip
        """All items of a list endpoint; a plain JSON array is one page. A paged object
        (`{"data"|"pods": [...], "nextCursor": ...}`) is followed to the end or raises."""
        p = _Parser("GET", template)
        out: list[Any] = []
        query = dict(params or {})
        for _ in range(MAX_PAGES):
            raw = await self._rest("GET", path, template, params=query)
            if raw is None:  # the real API answers JSON `null` for an empty list (L0 2026-11-04)
                return out
            if isinstance(raw, list):
                return out + raw
            d = p.obj(raw, "page")
            items = d.get("data", d.get("pods", d.get("items")))
            out += p.arr(items, "page.items")
            cursor = d.get("nextCursor")
            if cursor is None:
                return out
            if not isinstance(cursor, str) or not cursor:
                raise p.fail("nextCursor")
            query = {**dict(params or {}), "cursor": cursor}
        raise p.fail("too_many_pages")

    # --- pods ----------------------------------------------------------------
    async def list_pods(self) -> list[PodInfo]:
        """Every pod. An item named `llmhub-*` is parsed strictly (it may be ours: a bad shape
        raises). Any other item that does not parse (openapi `Pod` has no `required`: no
        string `name`, an odd `costPerHr`...) is skipped as foreign: a malformed foreign pod
        must not blind the reaper or `max_live_pods` (R3-6)."""
        p = _Parser("GET", "/pods")
        out: list[PodInfo] = []
        skipped = 0
        for x in await self._list("/pods", "/pods"):
            name = x.get("name") if isinstance(x, dict) else None
            if isinstance(name, str) and name.startswith(OUR_PREFIX):
                out.append(parse_pod(x, p))
                continue
            try:
                out.append(parse_pod(x, p))
            except RunpodParseError:
                skipped += 1
        if skipped:
            _log.warning("runpod: %d malformed foreign pod(s) skipped in GET /pods", skipped)
        return out

    async def get_pod(self, pod_id: str, *, trust_unlisted: bool = True) -> PodInfo | None:
        """None = no such pod. The openapi documents 400 and 404 for GET /pods/{id}: a 400 is
        checked against the full list (R2-7): not listed -> None (or `UnlistedPodError` when
        `trust_unlisted` is False: the list may lag behind a young pod, R3-5), else the 400
        is raised."""
        try:
            raw = await self._rest("GET", f"/pods/{_seg(pod_id, 'GET')}", "/pods/{id}",
                                   allow_404=True)  # fmt: skip
        except RunpodHttpError as exc:
            if exc.status != 400 or isinstance(exc, RunpodParseError):
                raise
            listed = next((x for x in await self.list_pods() if x.id == pod_id), None)
            if listed is None:
                if trust_unlisted:
                    return None
                raise UnlistedPodError(method=exc.method, path=exc.path, status=400,
                                       code="unlisted", retry_after_s=None,
                                       sent=True) from None  # fmt: skip
            raise
        return None if raw is None else parse_pod(raw, _Parser("GET", "/pods/{id}"))

    async def create_pod(self, body: Mapping[str, Any]) -> PodInfo:
        raw = await self._rest("POST", "/pods", "/pods", body=body)
        return parse_pod(raw, _Parser("POST", "/pods"))

    async def delete_pod(self, pod_id: str, *, trust_unlisted: bool = True) -> bool:
        """True = accepted, False = already gone. The openapi documents DELETE /pods/{id} ->
        204/400/401 (no 404): a 400 (or 404) is checked with `get_pod` (REV-S4/R2-7); gone
        or TERMINATED -> False, otherwise the original error is raised. `trust_unlisted`
        is passed to `get_pod` (R3-5)."""
        try:
            await self._rest("DELETE", f"/pods/{_seg(pod_id, 'DELETE')}", "/pods/{id}")
        except RunpodHttpError as exc:
            if exc.status not in (400, 404) or isinstance(exc, RunpodParseError):
                raise
            pod = await self.get_pod(pod_id, trust_unlisted=trust_unlisted)
            if pod is None or pod.desired_status.upper() == "TERMINATED":
                return False
            raise
        return True

    # --- read-only views (report only; never created or deleted, F4) ------------
    async def list_network_volumes(self) -> list[NamedRef]:
        p = _Parser("GET", "/networkvolumes")
        return [_named(x, p) for x in await self._list("/networkvolumes", "/networkvolumes")]

    async def list_templates(self) -> list[NamedRef]:
        """The account's own templates (public and RunPod ones are not ours)."""
        params = {"includePublicTemplates": "false", "includeRunpodTemplates": "false"}
        p = _Parser("GET", "/templates")
        return [_named(x, p) for x in await self._list("/templates", "/templates", params)]

    # --- GraphQL ---------------------------------------------------------------
    async def gpu_types(self, ids: Sequence[str]) -> list[GpuTypeInfo]:
        out: list[GpuTypeInfo] = []
        p = _Parser("POST", "graphql:gpuTypes")
        for gid in ids:
            query = f"query {{ gpuTypes(input: {{id: {json.dumps(gid)}}}) {{ {_GPU_FIELDS} }} }}"
            data = await self._graphql(query, "graphql:gpuTypes")
            for raw in p.arr(data.get("gpuTypes"), "gpuTypes"):
                d = p.obj(raw, "gpuType")
                community = d.get("communityCloud")
                if community is not None and not isinstance(community, bool):
                    raise p.fail("communityCloud")
                price = p.money(d, "communityPrice")  # number or numeric string
                if community is not True or price is None or price <= 0:
                    price = None  # no Community offer for this type
                low = d.get("lowestPrice")
                stock: str | None = None
                if low is not None:
                    stock = p.string(p.obj(low, "lowestPrice"), "stockStatus", optional=True)
                out.append(GpuTypeInfo(
                    id=p.req_str(d, "id"), display_name=p.req_str(d, "displayName"),
                    memory_gb=int(p.req_num(d, "memoryInGb")), community_price=price,
                    stock_status=stock,
                ))  # fmt: skip
        return out

    async def community_stock(self, ids: Sequence[str]) -> dict[str, str | None] | None:
        """id -> Community `stockStatus` (`lowestPrice(input: {gpuCount: 1, secureCloud:
        false})`; None = no stock shown) for the asked ids, in ONE GraphQL call over ALL types.
        Returns None when NO type of the whole answer shows any stock: such an answer carries no
        stock information (L1 2026-11-04 recorded null for every type, while at night on
        2026-11-05 the same query showed stock for 3090/4090 most minutes), so it must not
        block a POST. A value is necessary, not sufficient: a POST may still find no machine."""
        query = ("query { gpuTypes { id lowestPrice(input: {gpuCount: 1, secureCloud: false})"
                 " { stockStatus } } }")  # fmt: skip
        data = await self._graphql(query, "graphql:gpuStock")
        p = _Parser("POST", "graphql:gpuStock")
        wanted = set(ids)
        out: dict[str, str | None] = dict.fromkeys(ids)
        informative = False
        for raw in p.arr(data.get("gpuTypes"), "gpuTypes"):
            d = p.obj(raw, "gpuType")
            gid = p.req_str(d, "id")
            low = d.get("lowestPrice")
            stock = None if low is None else p.string(p.obj(low, "lowestPrice"), "stockStatus",
                                                       optional=True)  # fmt: skip
            informative = informative or bool(stock)
            if gid in wanted:
                out[gid] = stock or None
        return out if informative else None

    async def myself(self) -> AccountInfo:
        """GraphQL is not in the openapi (R2-10, verified only in L1). `spendLimit` may not
        exist: a GraphQL error asks again without it (`spend_limit=None`)."""
        try:
            data = await self._graphql(
                "query { myself { clientBalance currentSpendPerHr spendLimit } }",
                "graphql:myself",
            )
        except RunpodHttpError as exc:
            if exc.status != 200 or isinstance(exc, RunpodParseError):
                raise
            data = await self._graphql("query { myself { clientBalance currentSpendPerHr } }",
                                       "graphql:myself")  # fmt: skip
        p = _Parser("POST", "graphql:myself")
        me = p.obj(data.get("myself"), "myself")
        return AccountInfo(
            balance_usd=p.req_num(me, "clientBalance"),
            spend_per_hr=p.req_num(me, "currentSpendPerHr"),
            spend_limit=p.number(me, "spendLimit", optional=True),
        )

    async def billing_pods(self, start: float, end: float) -> list[BillingBucket]:
        params = {"startTime": iso(start), "endTime": iso(end), "bucketSize": "hour",
                  "grouping": "podId"}  # fmt: skip
        p = _Parser("GET", "/billing/pods")
        out: list[BillingBucket] = []
        for raw in await self._list("/billing/pods", "/billing/pods", params):
            d = p.obj(raw, "bucket")
            t = p.timestamp(d, "time", optional=False)
            assert t is not None
            out.append(BillingBucket(
                pod_id=p.string(d, "podId", optional=True), start=t, end=t + 3600.0,
                amount_usd=p.req_num(d, "amount"),
            ))  # fmt: skip
        return out


def _seg(pod_id: str, method: str) -> str:
    """A pod id as one path segment (ids are [A-Za-z0-9_-]; anything else is refused)."""
    if not pod_id or not pod_id.replace("-", "").replace("_", "").isalnum() or not pod_id.isascii():
        raise RunpodHttpError(method=method, path="/pods/{id}", status=None, code="bad_id",
                              retry_after_s=None, sent=False)  # fmt: skip
    return pod_id
