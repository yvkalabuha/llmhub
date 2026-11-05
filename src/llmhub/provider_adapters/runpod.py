"""RunpodAdapter (T-372 plan 3.3): Community Cloud GPU pods behind the ProviderAdapter protocol.

Safety rules (each one has a test in tests/test_t372_runpod_adapter.py):
1. `create` sends at most ONE `POST /pods` per (GPU type, allowed CUDA version), never retried
   (L2b): the GPU types of the offer's class (`gpu_type_ids`, a str or an ordered list) whose
   current Community price (`communityPrice` with `communityCloud` true, R3-3) is
   <= min(spec.max_price_usd_h, price_ceiling_usd_h) are tried cheapest first, and for each the
   versions one at a time, highest first, each with `allowedCudaVersions: [v]` (a list of
   several versions narrows the choice on the real API); the next attempt only after a
   definite `no_capacity` refusal; success, any unknown outcome or any other refusal stops.
   Before the first POST (once): the image is pinned by digest, the transport is `proxy`, the
   offer is an allowed GPU type, at least one type is within the cap, and fewer than `max_live_pods`
   `llmhub-*` pods exist in ANY status except TERMINATED (EXITED/CREATED/RESTARTING count,
   critic F6). A failed pre-check (including a failed listing) is a definite refusal
   (`outcome_unknown=False`). Stock pre-check (`stock_precheck`, one GraphQL call before the
   REST pod count): types without Community `stockStatus` get no POST; none in stock ->
   GPU_UNAVAILABLE without POST; an answer without stock for any type does not block. A 429
   (or the client's REST limiter: hold, POST budget) is a definite `rate_limited` refusal
   (the gateway refused before processing), never an unknown outcome; it stops the loop.
   After a 2xx the answer is checked (REV-S10): name == label,
   `costPerHr` <= ceiling, `volumeInGb` 0, not `interruptible`, no network volume. A mismatch
   raises `CreatedResourceRejected` WITHOUT deleting: the caller stores the ref in the DB,
   records a critical incident and destroys it (a foreign-named pod is found by nothing else).
2. `list_owned` = pods + network volumes (`vol:`) + templates (`tpl:`) with the prefix; any
   failed list or page raises: never a partial list, never `[]` on failure.
3. `destroy` deletes pods only (2xx -> ACCEPTED; 400/404 and `GET` says gone or TERMINATED ->
   ALREADY_GONE, REV-S4); `GET` 400 + "not in the list" counts as gone only when the pod was
   first known >= `list_staleness_s` ago (R3-5; else a retryable unknown error);
   `vol:`/`tpl:` refs are refused without an HTTP call (F4: we never create them, so they
   are a stop condition).
4. The pod `env` is never parsed or kept (C11); errors carry no body and no key.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Sequence
from typing import Any

from llmhub.clock import Clock
from llmhub.configuration.models import IMAGE_DIGEST_RE, RunpodConfig
from llmhub.provider_adapters.base import (
    Balance,
    BillingRecord,
    Capability,
    CreatedResourceRejected,
    DestroyResult,
    InstanceSpec,
    NormalizedError,
    Offer,
    OfferQuery,
    ProviderError,
    RemoteResource,
    RemoteStatus,
    ResourceKind,
    UnavailableReason,
)
from llmhub.provider_adapters.labels import parse_label
from llmhub.provider_adapters.rate_limit import POST_BUDGET, RATE_LIMITED, RestLimiter
from llmhub.provider_adapters.runpod_api import (
    GpuTypeInfo,
    NamedRef,
    PodInfo,
    RunpodApi,
    RunpodHttpError,
    RunpodParseError,
    UnlistedPodError,
)

log = logging.getLogger("llmhub.runpod")

REF_VOLUME = "vol:"
REF_TEMPLATE = "tpl:"
DEFAULT_PORT = 8080
HOURS_PER_MONTH = 730.0
_STATUS = {
    "RUNNING": RemoteStatus.RUNNING,
    "CREATED": RemoteStatus.PENDING,
    "RESTARTING": RemoteStatus.PENDING,
    "EXITED": RemoteStatus.EXITED,
    "TERMINATED": RemoteStatus.TERMINATED,
}


def pod_status(pod: PodInfo) -> RemoteStatus:
    return _STATUS.get(pod.desired_status.upper(), RemoteStatus.UNKNOWN)


def pod_endpoint(pod: PodInfo, port: int) -> str:
    """RunPod HTTP proxy (TLS terminated by RunPod); `direct` is out of scope in T-372."""
    return f"https://{pod.id}-{port}.proxy.runpod.net"


def is_live(pod: PodInfo) -> bool:
    """Counts against `max_live_pods`: every status except TERMINATED (critic F6)."""
    return pod.desired_status.upper() != "TERMINATED"


def created_mismatch(pod: PodInfo, label: str, ceiling: float) -> str | None:
    """What makes a just-created pod unacceptable (REV-S10), or None. A field missing from the
    answer is not a mismatch (the body set it); a contrary value is."""
    if pod.name != label:
        return "name"
    if pod.odd_fields:
        return "type of " + ",".join(pod.odd_fields)
    if pod.cost_per_hr is None or pod.cost_per_hr > ceiling:
        return f"price {pod.cost_per_hr}"
    if pod.volume_in_gb not in (None, 0):
        return f"volumeInGb {pod.volume_in_gb:g}"
    if pod.interruptible is True:
        return "interruptible"
    if pod.network_volume_id is not None:
        return "networkVolume"
    return None


def spec_port(spec: InstanceSpec) -> int:
    raw = spec.env.get("LLMHUB_PORT")
    try:
        return int(raw) if raw else DEFAULT_PORT
    except ValueError:
        return DEFAULT_PORT


def _cuda_key(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return ()


def cuda_order(versions: Sequence[str]) -> list[str]:
    """Allowed CUDA versions, highest first, without duplicates ("12.10" > "12.9")."""
    return sorted(dict.fromkeys(versions), key=_cuda_key, reverse=True)


def create_body(
    spec: InstanceSpec,
    label: str,
    cfg: RunpodConfig,
    gpu_type_id: str,
    *,
    docker_start_cmd: Sequence[str] | None = None,
    cuda_versions: Sequence[str] | None = None,
) -> dict[str, Any]:
    """`POST /pods` body. Never `networkVolumeId`, `templateId`, `containerRegistryAuthId`."""
    body: dict[str, Any] = {
        "name": label,
        "imageName": spec.image,
        "cloudType": cfg.cloud_type,
        "computeType": "GPU",
        "gpuTypeIds": [gpu_type_id],
        "gpuCount": 1,
        "containerDiskInGb": spec.disk_gb,
        "volumeInGb": 0,
        "ports": [f"{spec_port(spec)}/http"],
        "interruptible": False,
        "allowedCudaVersions": list(cfg.allowed_cuda_versions if cuda_versions is None
                                    else cuda_versions),
        "env": {**dict(spec.env), **dict(spec.secret_env)},
    }
    if docker_start_cmd is not None:
        body["dockerStartCmd"] = list(docker_start_cmd)
    return body


def _err(
    reason: UnavailableReason,
    message: str,
    *,
    retryable: bool = False,
    unknown: bool = False,
    status: int | None = None,
    retry_after: float | None = None,
    rate_limited: bool = False,
) -> NormalizedError:
    return NormalizedError(reason=reason, retryable=retryable, outcome_unknown=unknown,
                           retry_after_s=retry_after, http_status=status,
                           message=message[:200], rate_limited=rate_limited)  # fmt: skip


def _refuse(reason: UnavailableReason, message: str, *, retryable: bool = False) -> ProviderError:
    return ProviderError(_err(reason, f"refused: {message}", retryable=retryable))


def normalize_runpod_error(exc: BaseException) -> NormalizedError:
    """Plan 3.4. The message is `str(RunpodHttpError)` (method, path template, status, code)
    or the exception type name: never a body, a key or the exception text."""
    if isinstance(exc, ProviderError):
        return exc.err
    if isinstance(exc, RunpodParseError):
        return _err(UnavailableReason.PROVIDER_API_ERROR, str(exc), unknown=True)
    if isinstance(exc, UnlistedPodError):  # R3-5: gone or not listed yet: ask again later
        return _err(UnavailableReason.PROVIDER_API_ERROR, str(exc), retryable=True,
                    unknown=True, status=exc.status)  # fmt: skip
    if isinstance(exc, RunpodHttpError):
        st, code, msg = exc.status, exc.code, str(exc)
        if st in (401, 403):
            return _err(UnavailableReason.PROVIDER_API_ERROR, msg, status=st)
        if code == "insufficient_balance" or st == 402:
            return _err(UnavailableReason.INSUFFICIENT_BALANCE, msg, status=st)
        if code == "no_capacity":
            return _err(UnavailableReason.GPU_UNAVAILABLE, msg, retryable=True, status=st)
        if code == "spend_limit":
            return _err(UnavailableReason.QUOTA_EXCEEDED, msg, status=st)
        if st == 429 or code in (RATE_LIMITED, POST_BUDGET):
            # The gateway (or our own limiter, nothing sent) refused the request before it was
            # processed: a definite refusal, also for POST /pods (never an unknown outcome).
            return _err(UnavailableReason.PROVIDER_API_ERROR, msg, retryable=True, status=st,
                        retry_after=exc.retry_after_s, rate_limited=True)  # fmt: skip
        if st == 200:  # GraphQL errors (reads only)
            return _err(UnavailableReason.PROVIDER_API_ERROR, msg, retryable=True, status=st)
        if st is not None and 400 <= st < 500:
            return _err(UnavailableReason.DEPLOYMENT_ERROR, msg, status=st)
        if st is not None and st >= 500:
            return _err(UnavailableReason.PROVIDER_API_ERROR, msg, retryable=True, unknown=True,
                        status=st)  # fmt: skip
        return _err(UnavailableReason.PROVIDER_API_ERROR, msg, retryable=True, unknown=exc.sent)
    return _err(UnavailableReason.PROVIDER_API_ERROR, type(exc).__name__, retryable=True,
                unknown=True)  # fmt: skip


class RunpodAdapter:
    name = "runpod"
    capabilities = (
        Capability.OFFER_SEARCH
        | Capability.BALANCE_API
        | Capability.BILLING_API
        | Capability.CUSTOM_IMAGE
        | Capability.SPEND_RATE
    )

    def __init__(
        self,
        api: RunpodApi,
        cfg: RunpodConfig,
        clock: Clock,
        *,
        disk_gb_hint: int,
        port: int = DEFAULT_PORT,
    ) -> None:
        self.api = api
        self.cfg = cfg
        self.clock = clock
        self.disk_gb_hint = disk_gb_hint
        self.port = port
        # R3-5: pod id -> first time this process knew it (create answer, first get/destroy).
        # "400 and not in the list" means gone only once the list had time to show the pod.
        self._first_known: dict[str, float] = {}
        # (gpu type id, cuda version, result) of the POSTs of the last `create` (probe report)
        self.last_create_attempts: list[tuple[str, str, str]] = []

    def __repr__(self) -> str:
        return f"RunpodAdapter(cloud_type={self.cfg.cloud_type!r})"

    @property
    def list_staleness_s(self) -> float:
        return float(self.cfg.list_staleness_s)

    @property
    def rest_limiter(self) -> RestLimiter | None:
        """The REST limiter of the client (shared through the DB by GuardedAdapter)."""
        return self.api.limiter

    def _trust_unlisted(self, ref: str) -> bool:
        """True when `ref` was first known at least `list_staleness_s` ago (R3-5)."""
        now = self.clock.now()
        horizon = max(self.list_staleness_s * 10, 86400.0)
        if len(self._first_known) > 1024:  # forget very old ids (bounded memory)
            self._first_known = {k: t for k, t in self._first_known.items()
                                 if now - t < horizon}  # fmt: skip
        first = self._first_known.setdefault(ref, now)
        return now - first >= self.list_staleness_s

    def _disk_usd_h(self, disk_gb: int) -> float:
        return disk_gb * self.cfg.container_disk_usd_gb_month / HOURS_PER_MONTH

    # --- conversions -----------------------------------------------------------
    def _pod(self, pod: PodInfo, gpu_type_id: str | None = None) -> RemoteResource:
        status = pod_status(pod)
        return RemoteResource(
            provider=self.name, ref=pod.id, label=pod.name, kind=ResourceKind.INSTANCE,
            status=status, endpoint=pod_endpoint(pod, self.port), host_id=pod.machine_id,
            price_usd_h=pod.cost_per_hr or 0.0,
            spend_rate_usd_h=pod.cost_per_hr if status == RemoteStatus.RUNNING else None,
            created_at=pod.created_at if pod.created_at is not None else self.clock.now(),
            offer_id=gpu_type_id,
        )  # fmt: skip

    def _named(self, ref: NamedRef, prefix: str, kind: ResourceKind) -> RemoteResource:
        return RemoteResource(
            provider=self.name, ref=prefix + ref.id, label=ref.name, kind=kind,
            status=RemoteStatus.RUNNING, endpoint=None, host_id=None, price_usd_h=0.0,
            spend_rate_usd_h=None, created_at=self.clock.now(),
        )  # fmt: skip

    async def _call[T](self, coro: Awaitable[T]) -> T:
        try:
            return await coro
        except RunpodHttpError as exc:
            raise ProviderError(normalize_runpod_error(exc)) from None

    # --- ProviderAdapter ----------------------------------------------------------
    async def _priced(self, ids: Sequence[str]) -> list[tuple[str, GpuTypeInfo]]:
        """(id, info) of the types with a Community price (R3-3), cheapest first; ties keep
        the config order."""
        infos = await self.api.gpu_types(list(ids))
        by_id = {g.id: g for g in infos if g.id in ids and g.community_price is not None}
        ranked = [(i, by_id[i]) for i in ids if i in by_id]
        return sorted(ranked, key=lambda p: p[1].community_price or 0.0)  # stable: config order

    async def list_offers(self, q: OfferQuery) -> list[Offer]:
        """One offer per GPU type of the class with a Community price <= the pool cap."""
        ids = self.cfg.gpu_ids(q.gpu)
        if not ids:
            return []
        priced = await self._call(self._priced(ids))
        return [
            Offer(
                provider=self.name, offer_id=g.id, gpu=q.gpu, gpu_count=1, vram_gb=g.memory_gb,
                price_usd_h=price, disk_usd_h=self._disk_usd_h(self.disk_gb_hint),
                host_id="", interruptible=False, public_ip=False,
            )
            for _, g in priced
            if (price := g.community_price) is not None and price <= q.max_price_usd_h
        ]  # fmt: skip

    async def create(
        self,
        spec: InstanceSpec,
        label: str,
        *,
        docker_start_cmd: Sequence[str] | None = None,
    ) -> RemoteResource:
        self.last_create_attempts = []
        if parse_label(label) is None:
            raise _refuse(UnavailableReason.DEPLOYMENT_ERROR, "label is not an llmhub label")
        if not IMAGE_DIGEST_RE.match(spec.image):
            raise _refuse(UnavailableReason.DEPLOYMENT_ERROR, "image is not pinned by digest")
        if spec.env.get("LLMHUB_TRANSPORT", "proxy") != "proxy":
            raise _refuse(UnavailableReason.DEPLOYMENT_ERROR, "transport must be proxy")
        gpu_class = self.cfg.class_of(spec.offer_id)
        if gpu_class is None:
            raise _refuse(UnavailableReason.DEPLOYMENT_ERROR, "gpu type not allowed")
        ceiling = min(spec.max_price_usd_h, self.cfg.price_ceiling_usd_h)
        try:
            priced = await self._priced(self.cfg.gpu_ids(gpu_class))
        except RunpodHttpError as exc:
            if normalize_runpod_error(exc).rate_limited:
                raise ProviderError(normalize_runpod_error(exc)) from None
            raise _refuse(UnavailableReason.PROVIDER_API_ERROR, f"price check: {exc}",
                          retryable=True) from None  # fmt: skip
        # R3-3: stockStatus is a hint only; no capacity -> POST `no_capacity` (a definite refusal)
        if not priced:
            raise _refuse(UnavailableReason.GPU_UNAVAILABLE, "no community offer", retryable=True)
        gpus = [gid for gid, g in priced if (g.community_price or 0.0) <= ceiling]
        if not gpus:
            cheapest = priced[0][1].community_price or 0.0
            raise _refuse(UnavailableReason.DEPLOYMENT_ERROR,
                          f"price {cheapest:.3f} above ceiling {ceiling:.3f}")  # fmt: skip
        if self.cfg.stock_precheck:  # GraphQL, before the REST pod count (REST budget)
            gpus = await self._in_stock(gpus)
        try:
            pods = await self.api.list_pods()
        except RunpodHttpError as exc:
            if normalize_runpod_error(exc).rate_limited:
                raise ProviderError(normalize_runpod_error(exc)) from None
            raise _refuse(UnavailableReason.PROVIDER_API_ERROR, f"live pod count: {exc}",
                          retryable=True) from None  # fmt: skip
        live = [p for p in pods if p.name.startswith("llmhub-") and is_live(p)]
        if len(live) >= self.cfg.max_live_pods:
            raise _refuse(UnavailableReason.QUOTA_EXCEEDED,
                          f"{len(live)} llmhub pods exist (max_live_pods={self.cfg.max_live_pods})")  # fmt: skip
        versions = cuda_order(self.cfg.allowed_cuda_versions)
        if not versions:
            raise _refuse(UnavailableReason.DEPLOYMENT_ERROR, "no allowed cuda versions")
        gpu_id, pod = await self._post_each(spec, label, gpus, versions, docker_start_cmd)
        self._first_known.setdefault(pod.id, self.clock.now())
        what = created_mismatch(pod, label, ceiling)
        if what is not None:
            log.error("runpod: created pod %s has unexpected %s; the caller destroys it",
                      pod.id, what)  # fmt: skip
            raise CreatedResourceRejected(_err(
                UnavailableReason.DEPLOYMENT_ERROR,
                f"created pod {pod.id} has unexpected {what}; destroy it", unknown=True,
            ), self._pod(pod, gpu_id), what)  # fmt: skip
        return self._pod(pod, gpu_id)

    async def _in_stock(self, gpus: list[str]) -> list[str]:
        """Stock pre-check (one GraphQL call): only types whose Community `stockStatus` is
        set get a POST. No type of the class in stock -> a definite GPU_UNAVAILABLE without any
        POST. A failed check, or an answer without any stock for ANY type (no information),
        does not block: the POST decides, as before."""
        try:
            stock = await self.api.community_stock(gpus)
        except RunpodHttpError as exc:
            log.info("runpod: stock pre-check failed (%s); POSTing anyway", exc)
            return gpus
        if stock is None:
            log.info("runpod: stock pre-check shows no stock for any type; POSTing anyway")
            return gpus
        keep = [g for g in gpus if stock.get(g)]
        for g in gpus:
            if g not in keep:
                self.last_create_attempts.append((g, "", "no_stock"))
        if not keep:
            raise _refuse(UnavailableReason.GPU_UNAVAILABLE,
                          f"no community stock ({'; '.join(gpus)})", retryable=True)  # fmt: skip
        return keep

    async def _post_each(
        self,
        spec: InstanceSpec,
        label: str,
        gpus: Sequence[str],
        versions: Sequence[str],
        docker_start_cmd: Sequence[str] | None,
    ) -> tuple[str, PodInfo]:
        """One POST per (GPU type, version): types cheapest first, versions highest first;
        the next only after a definite `no_capacity`. Never a second POST after an unknown
        outcome."""
        last: NormalizedError | None = None
        for gid in gpus:
            for v in versions:
                body = create_body(spec, label, self.cfg, gid,
                                   docker_start_cmd=docker_start_cmd, cuda_versions=[v])  # fmt: skip
                try:
                    pod = await self.api.create_pod(body)
                except RunpodHttpError as exc:
                    err = normalize_runpod_error(exc)
                    no_cap = exc.code == "no_capacity" and not err.outcome_unknown
                    result = ("no_capacity" if no_cap
                              else "unknown" if err.outcome_unknown
                              else str(exc.code) if err.rate_limited and exc.code
                              else err.reason.value)  # fmt: skip
                    self.last_create_attempts.append((gid, v, result))
                    if not no_cap:
                        raise ProviderError(err) from None
                    log.info("runpod: no capacity for %s with cuda %s", gid, v)
                    last = err
                    continue
                self.last_create_attempts.append((gid, v, "created"))
                return gid, pod
        assert last is not None
        tried = ",".join(versions)
        raise ProviderError(_err(UnavailableReason.GPU_UNAVAILABLE,
                                 f"{last.message} ({'; '.join(gpus)}; cuda {tried})",
                                 retryable=True, status=last.http_status))  # fmt: skip

    async def get(self, ref: str) -> RemoteResource | None:
        if ref.startswith((REF_VOLUME, REF_TEMPLATE)):
            for r in await self._extras():
                if r.ref == ref:
                    return r
            return None
        trust = self._trust_unlisted(ref)
        pod = await self._call(self.api.get_pod(ref, trust_unlisted=trust))
        return None if pod is None else self._pod(pod)

    async def _extras(self) -> list[RemoteResource]:
        vols = await self._call(self.api.list_network_volumes())
        tpls = await self._call(self.api.list_templates())
        return [self._named(v, REF_VOLUME, ResourceKind.VOLUME) for v in vols] + [
            self._named(t, REF_TEMPLATE, ResourceKind.OTHER) for t in tpls
        ]

    async def list_owned(self, prefix: str = "llmhub-") -> list[RemoteResource]:
        pods = await self._call(self.api.list_pods())
        owned = [self._pod(p) for p in pods if p.name.startswith(prefix) and is_live(p)]
        owned += [r for r in await self._extras() if r.label.startswith(prefix)]
        return owned

    async def destroy(self, ref: str) -> DestroyResult:
        if ref.startswith((REF_VOLUME, REF_TEMPLATE)):
            raise ProviderError(_err(
                UnavailableReason.DEPLOYMENT_ERROR,
                "refused: llmhub deletes pods only; remove volumes/templates by hand (F4)",
            ))  # fmt: skip
        trust = self._trust_unlisted(ref)
        deleted = await self._call(self.api.delete_pod(ref, trust_unlisted=trust))
        return DestroyResult.ACCEPTED if deleted else DestroyResult.ALREADY_GONE

    async def balance(self) -> Balance | None:
        me = await self._call(self.api.myself())
        return Balance(usd=me.balance_usd, at=self.clock.now())

    async def billing_since(self, ts: float) -> list[BillingRecord]:
        now = self.clock.now()
        buckets = await self._call(self.api.billing_pods(ts, now))
        return [
            BillingRecord(ref=b.pod_id, kind="gpu", from_ts=b.start, to_ts=b.end,
                          amount_usd=max(0.0, b.amount_usd))
            for b in buckets
        ]  # fmt: skip

    def normalize_error(self, exc: BaseException) -> NormalizedError:
        return normalize_runpod_error(exc)
