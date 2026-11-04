"""ProviderAdapter Protocol, Capability flags and shared provider types (ADR-0138).

There is intentionally no `stop`/`pause`/`start`: the only way to end billing is `destroy`.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


class Capability(enum.Flag):
    NATIVE_LABELS = enum.auto()
    OFFER_SEARCH = enum.auto()
    BALANCE_API = enum.auto()
    BILLING_API = enum.auto()
    TRAFFIC_BILLED = enum.auto()
    INTERRUPTIBLE = enum.auto()
    CUSTOM_IMAGE = enum.auto()
    PROVIDER_TTL = enum.auto()
    SPEND_RATE = enum.auto()  # extension of the section 6.5 list (T-368 plan, section 14)


class UnavailableReason(enum.StrEnum):
    INSUFFICIENT_BALANCE = "insufficient_balance"
    QUOTA_EXCEEDED = "quota_exceeded"
    PROVIDER_API_ERROR = "provider_api_error"
    GPU_UNAVAILABLE = "gpu_unavailable"
    DEPLOYMENT_ERROR = "deployment_error"
    BUDGET_LIMIT_REACHED = "budget_limit_reached"


class RemoteStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    EXITED = "exited"
    TERMINATED = "terminated"
    UNKNOWN = "unknown"


class ResourceKind(enum.StrEnum):
    INSTANCE = "instance"
    VOLUME = "volume"
    OTHER = "other"


class DestroyResult(enum.StrEnum):
    ACCEPTED = "accepted"
    ALREADY_GONE = "already_gone"


@dataclass(frozen=True)
class OfferQuery:
    gpu: str
    min_vram_gb: int
    max_price_usd_h: float
    exclude_hosts: frozenset[str]
    need_public_ip: bool = False


@dataclass(frozen=True)
class Offer:
    provider: str
    offer_id: str
    gpu: str
    gpu_count: int
    vram_gb: int
    price_usd_h: float
    disk_usd_h: float
    host_id: str
    interruptible: bool
    public_ip: bool


@dataclass(frozen=True)
class InstanceSpec:
    pool: str
    offer_id: str
    image: str
    disk_gb: int
    max_price_usd_h: float
    max_lifetime_s: int
    env: Mapping[str, str]  # non-secret only


@dataclass(frozen=True)
class RemoteResource:
    provider: str
    ref: str
    label: str
    kind: ResourceKind
    status: RemoteStatus
    endpoint: str | None
    host_id: str | None
    price_usd_h: float
    spend_rate_usd_h: float | None
    created_at: float


@dataclass(frozen=True)
class Balance:
    usd: float
    at: float


@dataclass(frozen=True)
class BillingRecord:
    ref: str | None
    kind: str
    from_ts: float
    to_ts: float
    amount_usd: float


@dataclass(frozen=True)
class NormalizedError:
    reason: UnavailableReason
    retryable: bool
    # True when the call may have taken effect (create: 5xx / timeout -> maybe created, L7)
    outcome_unknown: bool
    retry_after_s: float | None
    http_status: int | None
    message: str  # no secrets, no request text


class ProviderError(Exception):
    def __init__(self, err: NormalizedError) -> None:
        super().__init__(err.message)
        self.err = err


class ProviderAdapter(Protocol):
    name: str
    capabilities: Capability

    async def list_offers(self, q: OfferQuery) -> list[Offer]: ...

    async def create(self, spec: InstanceSpec, label: str) -> RemoteResource: ...

    async def get(self, ref: str) -> RemoteResource | None:
        """None means gone (404)."""
        ...

    async def list_owned(self, prefix: str = "llmhub-") -> list[RemoteResource]:
        """All resource kinds whose label starts with prefix."""
        ...

    async def destroy(self, ref: str) -> DestroyResult:
        """Idempotent; 404 -> ALREADY_GONE."""
        ...

    async def balance(self) -> Balance | None:
        """None without BALANCE_API."""
        ...

    async def billing_since(self, ts: float) -> list[BillingRecord]: ...

    def normalize_error(self, exc: BaseException) -> NormalizedError: ...
