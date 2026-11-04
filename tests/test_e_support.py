"""Shared helpers for stream E tests (no tests here)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from llmhub.configuration.models import BudgetConfig, Config, PoolConfig
from llmhub.persistence.db import connect, migrate
from llmhub.provider_adapters.base import (
    Balance,
    BillingRecord,
    Capability,
    DestroyResult,
    InstanceSpec,
    NormalizedError,
    Offer,
    OfferQuery,
    RemoteResource,
    UnavailableReason,
)

T0 = 1_793_750_400.0  # 2026-11-04T00:00Z
HOUR = 3600.0


def make_cfg(tmp: Path, **budget: object) -> Config:
    pools = {
        "p1": PoolConfig(
            name="p1", provider="mock", gpu="RTX4090", vram_gb=24, pp_tps=1200, tg_tps=78,
            image="i", disk_gb=50, models=["m1"], idle_timeout_s=300,
        ),
        "x1": PoolConfig(
            name="x1", provider="mock", gpu="RTX5090", vram_gb=32, pp_tps=1200, tg_tps=78,
            image="i", disk_gb=50, models=["m1"], idle_timeout_s=300, subbudget="exception",
        ),
    }  # fmt: skip
    return Config(data_dir=tmp, pools=pools, budget=BudgetConfig(**budget))  # type: ignore[arg-type]


def make_conn(tmp: Path) -> sqlite3.Connection:
    conn = connect(tmp / "state.db")
    migrate(conn)
    conn.execute(
        "INSERT INTO clients(id, name, key_hash, created_at) VALUES ('c1','c1','x',0),"
        "('c2','c2','x',0)"
    )
    return conn


def add_instance(
    conn: sqlite3.Connection,
    iid: str = "i1",
    pool: str = "p1",
    state: str = "IDLE",
    price: float | None = 0.36,
    created: float = T0,
    accrued_to: float | None = T0,
    provider: str = "mock",
    offer_json: str | None = None,
    **extra: object,
) -> str:
    cols = {
        "id": iid, "op_id": "op-" + iid, "pool": pool, "provider": provider,
        "label": "llmhub-" + iid, "state": state, "state_since": created, "created_at": created,
        "max_lifetime_at": created + 10800, "price_usd_h": price, "accrued_to": accrued_to,
        "offer_json": offer_json, "provider_ref": "ref-" + iid,
    }  # fmt: skip
    cols.update(extra)
    keys = ",".join(cols)
    conn.execute(
        f"INSERT INTO instances({keys}) VALUES ({','.join('?' * len(cols))})", list(cols.values())
    )
    return iid


def add_ledger(
    conn: sqlite3.Connection,
    amount: float,
    from_ts: float,
    to_ts: float | None = None,
    *,
    source: str = "estimate",
    provider: str = "mock",
    pool: str | None = "p1",
    instance_id: str | None = None,
    kind: str = "gpu",
) -> None:
    conn.execute(
        "INSERT INTO ledger(instance_id, provider, pool, kind, from_ts, to_ts, amount_usd,"
        " source, created_at) VALUES (?,?,?,?,?,?,?,?,0)",
        (instance_id, provider, pool, kind, from_ts, to_ts if to_ts is not None else from_ts + 60,
         amount, source),
    )  # fmt: skip


class FakeBillingAdapter:
    """Minimal adapter: balance + billing only."""

    capabilities = Capability.BALANCE_API | Capability.BILLING_API

    def __init__(self, name: str = "mock", balance: float = 20.0) -> None:
        self.name = name
        self.balance_usd = balance
        self.records: list[BillingRecord] = []

    async def list_offers(self, q: OfferQuery) -> list[Offer]:
        return []

    async def create(self, spec: InstanceSpec, label: str) -> RemoteResource:
        raise NotImplementedError

    async def get(self, ref: str) -> RemoteResource | None:
        return None

    async def list_owned(self, prefix: str = "llmhub-") -> list[RemoteResource]:
        return []

    async def destroy(self, ref: str) -> DestroyResult:
        return DestroyResult.ALREADY_GONE

    async def balance(self) -> Balance | None:
        return Balance(usd=self.balance_usd, at=T0)

    async def billing_since(self, ts: float) -> list[BillingRecord]:
        return [r for r in self.records if r.to_ts > ts]

    def normalize_error(self, exc: BaseException) -> NormalizedError:
        return NormalizedError(
            reason=UnavailableReason.PROVIDER_API_ERROR, retryable=True, outcome_unknown=False,
            retry_after_s=None, http_status=None, message="x",
        )  # fmt: skip
