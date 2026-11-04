"""MockProvider: deterministic in-process "cloud" (stage 3a).

S0 skeleton: create/get/list_owned/destroy without faults. `inject` and the remaining
failure helpers belong to stream B (B3) and raise NotImplementedError until then.
"""

from __future__ import annotations

import enum
import sqlite3
from dataclasses import dataclass
from typing import Literal

from llmhub.clock import Clock
from llmhub.provider_adapters.base import (
    Balance,
    BillingRecord,
    Capability,
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
from llmhub.provider_adapters.mock_store import MockStore


@dataclass
class MockKnobs:
    boot_s: float = 60
    price_usd_h: float = 0.22
    disk_usd_h: float = 0.0
    destroy_delay_s: float = 0
    list_owned_lag_s: float = 0  # created resources invisible for this long
    balance_usd: float = 25.0
    gpu_available: bool = True
    offers: int = 3


class Fault(enum.StrEnum):
    CREATE_TIMEOUT_CREATED = "create_timeout_created"
    CREATE_TIMEOUT_NOT_CREATED = "create_timeout_not_created"
    CREATE_TIMEOUT_CREATED_LATE = "create_timeout_created_late"
    CREATE_5XX = "create_5xx"
    CREATE_GHOST_REF = "create_ghost_ref"
    CREATE_HANG = "create_hang"
    RATE_LIMIT_429 = "rate_limit_429"
    API_5XX = "api_5xx"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    QUOTA = "quota"
    GPU_UNAVAILABLE = "gpu_unavailable"
    DESTROY_FAIL = "destroy_fail"
    DESTROY_NEVER = "destroy_never"
    DESTROY_429 = "destroy_429"
    DESTROY_PARTIAL = "destroy_partial"
    LIST_STALE = "list_stale"


class MockProvider:
    capabilities = (
        Capability.NATIVE_LABELS
        | Capability.OFFER_SEARCH
        | Capability.BALANCE_API
        | Capability.INTERRUPTIBLE
        | Capability.CUSTOM_IMAGE
        | Capability.SPEND_RATE
    )

    def __init__(
        self,
        store: MockStore,
        clock: Clock,
        knobs: MockKnobs | None = None,
        name: str = "mock",
    ) -> None:
        self.name = name
        self.store = store
        self.clock = clock
        self.knobs = knobs or MockKnobs()
        self.create_calls = 0

    # --- ProviderAdapter -------------------------------------------------
    async def list_offers(self, q: OfferQuery) -> list[Offer]:
        if not self.knobs.gpu_available:
            return []
        k = self.knobs
        return [
            Offer(
                provider=self.name, offer_id=f"offer-{i}", gpu=q.gpu, gpu_count=1,
                vram_gb=max(q.min_vram_gb, 24), price_usd_h=round(k.price_usd_h + 0.01 * i, 4),
                disk_usd_h=k.disk_usd_h, host_id=f"host-{i}", interruptible=False, public_ip=True,
            )
            for i in range(k.offers)
        ]  # fmt: skip

    async def create(self, spec: InstanceSpec, label: str) -> RemoteResource:
        self.create_calls += 1
        ref = self.store.next_ref()
        offer_idx = int(spec.offer_id.rsplit("-", 1)[-1]) if "-" in spec.offer_id else 0
        self.store.conn.execute(
            "INSERT INTO resources(ref,label,created_at,boot_s,price_usd_h,disk_usd_h,host_id)"
            " VALUES (?,?,?,?,?,?,?)",
            (ref, label, self.clock.now(), self.knobs.boot_s,
             round(self.knobs.price_usd_h + 0.01 * offer_idx, 4), self.knobs.disk_usd_h,
             f"host-{offer_idx}"),
        )  # fmt: skip
        res = await self.get(ref)
        assert res is not None
        return res

    async def get(self, ref: str) -> RemoteResource | None:
        row = self.store.conn.execute("SELECT * FROM resources WHERE ref=?", (ref,)).fetchone()
        if row is None or self._gone(row):
            return None
        return self._to_resource(row)

    async def list_owned(self, prefix: str = "llmhub-") -> list[RemoteResource]:
        now = self.clock.now()
        rows = self.store.conn.execute(
            "SELECT * FROM resources WHERE label LIKE ? ORDER BY ref", (prefix + "%",)
        ).fetchall()
        return [
            self._to_resource(r)
            for r in rows
            if not self._gone(r) and now >= r["created_at"] + self.knobs.list_owned_lag_s
        ]

    async def destroy(self, ref: str) -> DestroyResult:
        row = self.store.conn.execute("SELECT * FROM resources WHERE ref=?", (ref,)).fetchone()
        if row is None or self._gone(row):
            return DestroyResult.ALREADY_GONE
        if row["destroyed_at"] is None:
            self.store.conn.execute(
                "UPDATE resources SET destroyed_at=? WHERE ref=?", (self.clock.now(), ref)
            )
        return DestroyResult.ACCEPTED

    async def balance(self) -> Balance | None:
        now = self.clock.now()
        return Balance(usd=self.knobs.balance_usd - self.billed_total(), at=now)

    async def billing_since(self, ts: float) -> list[BillingRecord]:
        return []

    def normalize_error(self, exc: BaseException) -> NormalizedError:
        if isinstance(exc, ProviderError):
            return exc.err
        return NormalizedError(
            reason=UnavailableReason.PROVIDER_API_ERROR, retryable=True, outcome_unknown=False,
            retry_after_s=None, http_status=None, message=type(exc).__name__,
        )  # fmt: skip

    # --- test helpers ----------------------------------------------------
    def inject(
        self,
        op: Literal["create", "get", "list_owned", "destroy", "balance", "any"],
        fault: Fault,
        times: int = 1,
    ) -> None:
        raise NotImplementedError("fault injection: stream B (B3)")

    def interrupt(self, ref: str) -> None:
        self.store.conn.execute(
            "UPDATE resources SET status_override='terminated' WHERE ref=?", (ref,)
        )

    def stopped_but_billing(self, ref: str) -> None:
        raise NotImplementedError("stream B (B3)")

    def add_foreign(self, label: str) -> str:
        return self._add_raw(label)

    def add_orphan(self, label: str) -> str:
        return self._add_raw(label)

    def live_refs(self) -> list[str]:
        rows = self.store.conn.execute("SELECT * FROM resources ORDER BY ref").fetchall()
        return [r["ref"] for r in rows if not self._gone(r)]

    def billed_total(self, since: float = 0) -> float:
        now = self.clock.now()
        total = 0.0
        for r in self.store.conn.execute("SELECT * FROM resources"):
            end = now
            if r["destroyed_at"] is not None:
                end = min(now, r["destroyed_at"] + self.knobs.destroy_delay_s)
            start = max(r["created_at"], since)
            if end > start:
                total += (end - start) * (r["price_usd_h"] + r["disk_usd_h"]) / 3600
        return total

    # --- internals -------------------------------------------------------
    def _add_raw(self, label: str) -> str:
        ref = self.store.next_ref()
        self.store.conn.execute(
            "INSERT INTO resources(ref,label,created_at,boot_s,price_usd_h,host_id)"
            " VALUES (?,?,?,?,?,?)",
            (ref, label, self.clock.now(), 0.0, self.knobs.price_usd_h, "host-x"),
        )
        return ref

    def _gone(self, row: sqlite3.Row) -> bool:
        destroyed_at = row["destroyed_at"]
        return destroyed_at is not None and (
            self.clock.now() >= destroyed_at + self.knobs.destroy_delay_s
        )

    def _to_resource(self, row: sqlite3.Row) -> RemoteResource:
        r = row
        now = self.clock.now()
        if r["status_override"]:
            status = RemoteStatus(r["status_override"])
        elif now < r["created_at"] + r["boot_s"]:
            status = RemoteStatus.PENDING
        else:
            status = RemoteStatus.RUNNING
        ref = r["ref"]
        return RemoteResource(
            provider=self.name, ref=ref, label=r["label"], kind=ResourceKind.INSTANCE,
            status=status, endpoint=f"mock://{ref}", host_id=r["host_id"],
            price_usd_h=r["price_usd_h"], spend_rate_usd_h=None, created_at=r["created_at"],
        )  # fmt: skip
