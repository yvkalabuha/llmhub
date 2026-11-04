"""RunpodAdapter (T-372 plan 3.3): Community Cloud GPU pods behind the ProviderAdapter protocol.

Safety rules (each one has a test in tests/test_t372_runpod_adapter.py):
1. `create` sends at most ONE `POST /pods`, never retried. Before it: the image is pinned by
   digest, the transport is `proxy`, the offer is an allowed GPU type whose current Community
   price (`communityPrice` with `communityCloud` true, R3-3) is
   <= min(spec.max_price_usd_h, price_ceiling_usd_h), and fewer than `max_live_pods`
   `llmhub-*` pods exist in ANY status except TERMINATED (EXITED/CREATED/RESTARTING count,
   critic F6). A failed pre-check (including a failed listing) is a definite refusal
   (`outcome_unknown=False`). After a 2xx the answer is checked (REV-S10): name == label,
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
from llmhub.provider_adapters.runpod_api import (
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


def create_body(
    spec: InstanceSpec,
    label: str,
    cfg: RunpodConfig,
    gpu_type_id: str,
    *,
    docker_start_cmd: Sequence[str] | None = None,
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
        "allowedCudaVersions": list(cfg.allowed_cuda_versions),
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
) -> NormalizedError:
    return NormalizedError(reason=reason, retryable=retryable, outcome_unknown=unknown,
                           retry_after_s=retry_after, http_status=status,
                           message=message[:200])  # fmt: skip


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
        if st == 429:
            return _err(UnavailableReason.PROVIDER_API_ERROR, msg, retryable=True, status=st,
                        retry_after=exc.retry_after_s)  # fmt: skip
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

    def __repr__(self) -> str:
        return f"RunpodAdapter(cloud_type={self.cfg.cloud_type!r})"

    @property
    def list_staleness_s(self) -> float:
        return float(self.cfg.list_staleness_s)

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
    def _pod(self, pod: PodInfo) -> RemoteResource:
        status = pod_status(pod)
        return RemoteResource(
            provider=self.name, ref=pod.id, label=pod.name, kind=ResourceKind.INSTANCE,
            status=status, endpoint=pod_endpoint(pod, self.port), host_id=pod.machine_id,
            price_usd_h=pod.cost_per_hr or 0.0,
            spend_rate_usd_h=pod.cost_per_hr if status == RemoteStatus.RUNNING else None,
            created_at=pod.created_at if pod.created_at is not None else self.clock.now(),
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
    async def list_offers(self, q: OfferQuery) -> list[Offer]:
        gid = self.cfg.gpu_type_ids.get(q.gpu)
        if gid is None:
            return []
        infos = await self._call(self.api.gpu_types([gid]))
        out: list[Offer] = []
        for g in infos:
            if g.id != gid or g.community_price is None:
                continue  # no Community price (communityCloud false/null): no offer (R3-3)
            out.append(Offer(
                provider=self.name, offer_id=g.id, gpu=q.gpu, gpu_count=1, vram_gb=g.memory_gb,
                price_usd_h=g.community_price, disk_usd_h=self._disk_usd_h(self.disk_gb_hint),
                host_id="", interruptible=False, public_ip=False,
            ))  # fmt: skip
        return out

    async def create(
        self,
        spec: InstanceSpec,
        label: str,
        *,
        docker_start_cmd: Sequence[str] | None = None,
    ) -> RemoteResource:
        if parse_label(label) is None:
            raise _refuse(UnavailableReason.DEPLOYMENT_ERROR, "label is not an llmhub label")
        if not IMAGE_DIGEST_RE.match(spec.image):
            raise _refuse(UnavailableReason.DEPLOYMENT_ERROR, "image is not pinned by digest")
        if spec.env.get("LLMHUB_TRANSPORT", "proxy") != "proxy":
            raise _refuse(UnavailableReason.DEPLOYMENT_ERROR, "transport must be proxy")
        if spec.offer_id not in self.cfg.gpu_type_ids.values():
            raise _refuse(UnavailableReason.DEPLOYMENT_ERROR, "gpu type not allowed")
        ceiling = min(spec.max_price_usd_h, self.cfg.price_ceiling_usd_h)
        try:
            infos = await self.api.gpu_types([spec.offer_id])
        except RunpodHttpError as exc:
            raise _refuse(UnavailableReason.PROVIDER_API_ERROR, f"price check: {exc}",
                          retryable=True) from None  # fmt: skip
        info = next((g for g in infos if g.id == spec.offer_id), None)
        # R3-3: stockStatus is a hint only; no capacity -> POST `no_capacity` (a definite refusal)
        if info is None or info.community_price is None:
            raise _refuse(UnavailableReason.GPU_UNAVAILABLE, "no community offer", retryable=True)
        if info.community_price > ceiling:
            raise _refuse(UnavailableReason.DEPLOYMENT_ERROR,
                          f"price {info.community_price:.3f} above ceiling {ceiling:.3f}")  # fmt: skip
        try:
            pods = await self.api.list_pods()
        except RunpodHttpError as exc:
            raise _refuse(UnavailableReason.PROVIDER_API_ERROR, f"live pod count: {exc}",
                          retryable=True) from None  # fmt: skip
        live = [p for p in pods if p.name.startswith("llmhub-") and is_live(p)]
        if len(live) >= self.cfg.max_live_pods:
            raise _refuse(UnavailableReason.QUOTA_EXCEEDED,
                          f"{len(live)} llmhub pods exist (max_live_pods={self.cfg.max_live_pods})")  # fmt: skip
        body = create_body(spec, label, self.cfg, spec.offer_id, docker_start_cmd=docker_start_cmd)
        try:
            pod = await self.api.create_pod(body)
        except RunpodHttpError as exc:
            raise ProviderError(normalize_runpod_error(exc)) from None
        self._first_known.setdefault(pod.id, self.clock.now())
        what = created_mismatch(pod, label, ceiling)
        if what is not None:
            log.error("runpod: created pod %s has unexpected %s; the caller destroys it",
                      pod.id, what)  # fmt: skip
            raise CreatedResourceRejected(_err(
                UnavailableReason.DEPLOYMENT_ERROR,
                f"created pod {pod.id} has unexpected {what}; destroy it", unknown=True,
            ), self._pod(pod), what)  # fmt: skip
        return self._pod(pod)

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
