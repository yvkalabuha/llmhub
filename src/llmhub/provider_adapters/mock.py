"""MockProvider: deterministic in-process "cloud" (stage 3a) with fault injection.

All time comes from the injected clock; accruals are per second from create to
`destroyed_at + destroy_delay_s`. Faults are injected per operation with `inject`.
"""

from __future__ import annotations

import enum
import sqlite3
from dataclasses import dataclass, replace
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
    late_visible_s: float = 300  # CREATE_TIMEOUT_CREATED_LATE: invisible to list_owned this long
    stale_s: float = 300  # LIST_STALE: list_owned shows the world as of this many seconds ago
    hang_s: float = 3600  # CREATE_HANG: the call returns only after this long
    volume_usd_h: float = 0.01  # price of the volume left by DESTROY_PARTIAL
    # declared stale-list window (teardown needs two zeros this far apart); pair with LIST_STALE
    list_staleness_s: float = 0.0


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


_Op = Literal[
    "create", "get", "list_owned", "destroy", "balance", "any", "list_offers", "billing_since"
]

_ALL_OPS = frozenset({"create", "get", "list_owned", "destroy", "balance", "list_offers",
                      "billing_since"})  # fmt: skip
_APPLICABLE: dict[Fault, frozenset[str]] = {
    Fault.CREATE_TIMEOUT_CREATED: frozenset({"create"}),
    Fault.CREATE_TIMEOUT_NOT_CREATED: frozenset({"create"}),
    Fault.CREATE_TIMEOUT_CREATED_LATE: frozenset({"create"}),
    Fault.CREATE_5XX: frozenset({"create"}),
    Fault.CREATE_GHOST_REF: frozenset({"create"}),
    Fault.CREATE_HANG: frozenset({"create"}),
    Fault.RATE_LIMIT_429: _ALL_OPS,
    Fault.API_5XX: _ALL_OPS,
    Fault.INSUFFICIENT_BALANCE: frozenset({"create"}),
    Fault.QUOTA: frozenset({"create"}),
    Fault.GPU_UNAVAILABLE: frozenset({"create", "list_offers"}),
    Fault.DESTROY_FAIL: frozenset({"destroy"}),
    Fault.DESTROY_NEVER: frozenset({"destroy"}),
    Fault.DESTROY_429: frozenset({"destroy"}),
    Fault.DESTROY_PARTIAL: frozenset({"destroy"}),
    Fault.LIST_STALE: frozenset({"list_owned"}),
}


def _error(
    reason: UnavailableReason,
    message: str,
    *,
    status: int | None = None,
    retryable: bool = True,
    unknown: bool = False,
    retry_after: float | None = None,
) -> ProviderError:
    return ProviderError(
        NormalizedError(
            reason=reason,
            retryable=retryable,
            outcome_unknown=unknown,
            retry_after_s=retry_after,
            http_status=status,
            message=message,
        )
    )


@dataclass
class _Injected:
    op: str
    fault: Fault
    remaining: int


class MockProvider:
    capabilities = (
        Capability.NATIVE_LABELS
        | Capability.OFFER_SEARCH
        | Capability.BALANCE_API
        | Capability.BILLING_API
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
        self._faults: list[_Injected] = []

    @property
    def list_staleness_s(self) -> float:
        """Optional adapter attribute read by teardown.confirm_zero (stream C)."""
        return self.knobs.list_staleness_s

    # --- ProviderAdapter -------------------------------------------------
    async def list_offers(self, q: OfferQuery) -> list[Offer]:
        fault = self._check("list_offers")
        if fault == Fault.GPU_UNAVAILABLE or not self.knobs.gpu_available:
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
        self._enforce_balance()
        fault = self._check("create")
        if self._balance_left() <= 0:
            raise _error(UnavailableReason.INSUFFICIENT_BALANCE, "balance exhausted",
                         status=402, retryable=False)  # fmt: skip
        if fault == Fault.CREATE_5XX:
            raise _error(UnavailableReason.PROVIDER_API_ERROR, "create 5xx", status=503)
        if fault == Fault.CREATE_TIMEOUT_NOT_CREATED:
            raise _error(UnavailableReason.PROVIDER_API_ERROR, "create timeout", unknown=True)
        now = self.clock.now()
        if fault == Fault.CREATE_TIMEOUT_CREATED_LATE:
            self._insert(spec, label, visible_at=now + self.knobs.late_visible_s)
            raise _error(UnavailableReason.PROVIDER_API_ERROR, "create timeout", unknown=True)
        ref = self._insert(spec, label)
        if fault == Fault.CREATE_TIMEOUT_CREATED:
            raise _error(UnavailableReason.PROVIDER_API_ERROR, "create timeout", unknown=True)
        if fault == Fault.CREATE_HANG:
            await self.clock.sleep_until(now + self.knobs.hang_s)
        res = await self.get(ref)
        assert res is not None
        if fault == Fault.CREATE_GHOST_REF:
            return replace(res, ref=f"ghost-{ref}")
        return res

    async def get(self, ref: str) -> RemoteResource | None:
        self._enforce_balance()
        self._check("get")
        row = self._row(ref)
        if row is None or self._gone(row):
            return None
        return self._to_resource(row)

    async def list_owned(self, prefix: str = "llmhub-") -> list[RemoteResource]:
        self._enforce_balance()
        fault = self._check("list_owned")
        now = self.clock.now()
        at = now - self.knobs.stale_s if fault == Fault.LIST_STALE else now
        rows = self.store.conn.execute(
            "SELECT * FROM resources WHERE label LIKE ? ORDER BY ref", (prefix + "%",)
        ).fetchall()
        return [
            self._to_resource(r, at)
            for r in rows
            if r["created_at"] <= at and not self._gone(r, at) and self._visible(r, at)
        ]

    async def destroy(self, ref: str) -> DestroyResult:
        fault = self._check("destroy")
        row = self._row(ref)
        if row is None or self._gone(row):
            return DestroyResult.ALREADY_GONE
        conn = self.store.conn
        if fault == Fault.DESTROY_NEVER:
            conn.execute("UPDATE resources SET immortal=1 WHERE ref=?", (ref,))
        if row["immortal"] or fault == Fault.DESTROY_NEVER:
            return DestroyResult.ACCEPTED
        now = self.clock.now()
        if row["destroyed_at"] is None:
            conn.execute("UPDATE resources SET destroyed_at=? WHERE ref=?", (now, ref))
            if fault == Fault.DESTROY_PARTIAL and row["kind"] == "instance":
                self.store.insert(
                    label=row["label"], kind="volume", created_at=now, boot_s=0.0,
                    price_usd_h=self.knobs.volume_usd_h, host_id=row["host_id"],
                )  # fmt: skip
        return DestroyResult.ACCEPTED

    async def balance(self) -> Balance | None:
        self._enforce_balance()
        self._check("balance")
        return Balance(usd=self._balance_left(), at=self.clock.now())

    async def billing_since(self, ts: float) -> list[BillingRecord]:
        self._check("billing_since")
        now = self.clock.now()
        out: list[BillingRecord] = []
        for r in self.store.conn.execute("SELECT * FROM resources ORDER BY ref"):
            start, end = max(r["created_at"], ts), self._billed_until(r, now)
            if end > start:
                amount = (end - start) * (r["price_usd_h"] + r["disk_usd_h"]) / 3600
                out.append(BillingRecord(r["ref"], r["kind"], start, end, amount))
        return out

    def normalize_error(self, exc: BaseException) -> NormalizedError:
        if isinstance(exc, ProviderError):
            return exc.err
        return NormalizedError(
            reason=UnavailableReason.PROVIDER_API_ERROR, retryable=True,
            outcome_unknown=isinstance(exc, TimeoutError), retry_after_s=None,
            http_status=None, message=type(exc).__name__,
        )  # fmt: skip

    # --- test helpers ----------------------------------------------------
    def inject(self, op: _Op, fault: Fault, times: int = 1) -> None:
        """Make the next `times` matching calls of `op` ("any" = any applicable op) fail."""
        if op != "any" and op not in _APPLICABLE[fault]:
            raise ValueError(f"fault {fault} does not apply to {op}")
        self._faults.append(_Injected(op, fault, times))

    def interrupt(self, ref: str) -> None:
        self.store.conn.execute(
            "UPDATE resources SET status_override='terminated' WHERE ref=?", (ref,)
        )

    def stopped_but_billing(self, ref: str) -> None:
        """The provider "stopped" the instance but keeps billing it (status EXITED)."""
        self.store.conn.execute("UPDATE resources SET status_override='exited' WHERE ref=?", (ref,))

    def add_foreign(self, label: str) -> str:
        return self._add_raw(label)

    def add_orphan(self, label: str) -> str:
        return self._add_raw(label)

    def add_attached(self, label: str, kind: ResourceKind, price_usd_h: float = 0.01) -> str:
        """A billed non-instance resource (volume / other) carrying `label`."""
        return self.store.insert(
            label=label, kind=kind.value, created_at=self.clock.now(), boot_s=0.0,
            price_usd_h=price_usd_h, host_id="host-x",
        )  # fmt: skip

    def live_refs(self) -> list[str]:
        """Ground truth for tests, including resources invisible to list_owned."""
        rows = self.store.conn.execute("SELECT * FROM resources ORDER BY ref").fetchall()
        return [r["ref"] for r in rows if not self._gone(r)]

    def billed_total(self, since: float = 0) -> float:
        now = self.clock.now()
        total = 0.0
        for r in self.store.conn.execute("SELECT * FROM resources"):
            end = self._billed_until(r, now)
            start = max(r["created_at"], since)
            if end > start:
                total += (end - start) * (r["price_usd_h"] + r["disk_usd_h"]) / 3600
        return total

    # --- internals -------------------------------------------------------
    def _check(self, op: str) -> Fault | None:
        """Consume a matching injected fault; raise for the simple ones, return special ones."""
        fault: Fault | None = None
        for e in self._faults:
            if e.remaining > 0 and (e.op == op or (e.op == "any" and op in _APPLICABLE[e.fault])):
                e.remaining -= 1
                fault = e.fault
                break
        self._faults = [e for e in self._faults if e.remaining > 0]
        if fault == Fault.RATE_LIMIT_429:
            raise _error(UnavailableReason.PROVIDER_API_ERROR, "rate limited", status=429,
                         retry_after=5.0)  # fmt: skip
        if fault == Fault.API_5XX:
            raise _error(UnavailableReason.PROVIDER_API_ERROR, "api 5xx", status=503)
        if fault == Fault.INSUFFICIENT_BALANCE:
            raise _error(UnavailableReason.INSUFFICIENT_BALANCE, "insufficient balance",
                         status=402, retryable=False)  # fmt: skip
        if fault == Fault.QUOTA:
            raise _error(UnavailableReason.QUOTA_EXCEEDED, "quota exceeded", status=403,
                         retryable=False)  # fmt: skip
        if fault == Fault.GPU_UNAVAILABLE and op == "create":
            raise _error(UnavailableReason.GPU_UNAVAILABLE, "gpu unavailable", status=409,
                         retryable=False)  # fmt: skip
        if fault == Fault.DESTROY_FAIL:
            raise _error(UnavailableReason.PROVIDER_API_ERROR, "destroy failed", status=500)
        if fault == Fault.DESTROY_429:
            raise _error(UnavailableReason.PROVIDER_API_ERROR, "rate limited", status=429,
                         retry_after=30.0)  # fmt: skip
        return fault

    def _row(self, ref: str) -> sqlite3.Row | None:
        row = self.store.conn.execute("SELECT * FROM resources WHERE ref=?", (ref,)).fetchone()
        return row if isinstance(row, sqlite3.Row) else None

    def _insert(self, spec: InstanceSpec, label: str, visible_at: float = 0.0) -> str:
        offer_idx = int(spec.offer_id.rsplit("-", 1)[-1]) if "-" in spec.offer_id else 0
        return self.store.insert(
            label=label, created_at=self.clock.now(), boot_s=self.knobs.boot_s,
            price_usd_h=round(self.knobs.price_usd_h + 0.01 * offer_idx, 4),
            disk_usd_h=self.knobs.disk_usd_h, host_id=f"host-{offer_idx}", visible_at=visible_at,
        )  # fmt: skip

    def _add_raw(self, label: str) -> str:
        return self.store.insert(
            label=label, created_at=self.clock.now(), boot_s=0.0,
            price_usd_h=self.knobs.price_usd_h, host_id="host-x",
        )  # fmt: skip

    def _balance_left(self) -> float:
        return self.knobs.balance_usd - self.billed_total()

    def _enforce_balance(self) -> None:
        """Like RunPod: at zero balance every live resource is deleted."""
        if self._balance_left() > 0:
            return
        self.store.conn.execute(
            "UPDATE resources SET destroyed_at=?, immortal=0 WHERE destroyed_at IS NULL",
            (self.clock.now(),),
        )

    def _billed_until(self, row: sqlite3.Row, now: float) -> float:
        if row["destroyed_at"] is None:
            return now
        return min(now, row["destroyed_at"] + self.knobs.destroy_delay_s)

    def _visible(self, row: sqlite3.Row, at: float) -> bool:
        if row["kind"] != "instance":
            return True
        return at >= max(row["created_at"] + self.knobs.list_owned_lag_s, row["visible_at"])

    def _gone(self, row: sqlite3.Row, at: float | None = None) -> bool:
        t = self.clock.now() if at is None else at
        destroyed_at = row["destroyed_at"]
        return destroyed_at is not None and t >= destroyed_at + self.knobs.destroy_delay_s

    def _to_resource(self, row: sqlite3.Row, at: float | None = None) -> RemoteResource:
        r = row
        now = self.clock.now() if at is None else at
        kind = ResourceKind(r["kind"])
        if kind != ResourceKind.INSTANCE:
            status = RemoteStatus.RUNNING
        elif r["status_override"]:
            status = RemoteStatus(r["status_override"])
        elif now < r["created_at"] + r["boot_s"]:
            status = RemoteStatus.PENDING
        else:
            status = RemoteStatus.RUNNING
        ref = r["ref"]
        return RemoteResource(
            provider=self.name, ref=ref, label=r["label"], kind=kind,
            status=status, endpoint=f"mock://{ref}" if kind == ResourceKind.INSTANCE else None,
            host_id=r["host_id"], price_usd_h=r["price_usd_h"],
            spend_rate_usd_h=r["price_usd_h"] + r["disk_usd_h"], created_at=r["created_at"],
        )  # fmt: skip
