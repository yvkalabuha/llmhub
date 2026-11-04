"""Shared helpers for stream C tests: in-memory DB rows, a fake cloud, a fake inference."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness import default_config
from llmhub.configuration.models import Config
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
    ProviderError,
    RemoteResource,
    RemoteStatus,
    ResourceKind,
    UnavailableReason,
)

T0 = 1_000_000.0
POOL = "p1"


def make_conn(path: Path | str = ":memory:") -> sqlite3.Connection:
    conn = connect(path, busy_timeout_ms=300)
    migrate(conn)
    if conn.execute("SELECT count(*) FROM clients").fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO clients(id, name, key_hash, created_at, data_class)"
            " VALUES ('c1','c1','x',?,'cloud_allowed')",
            (T0,),
        )
    return conn


def make_cfg(tmp_path: Path | None = None) -> Config:
    return default_config(tmp_path or Path("/nonexistent"))


def add_instance(
    conn: sqlite3.Connection,
    iid: str,
    state: str,
    *,
    pool: str = POOL,
    provider: str = "fake",
    **cols: Any,
) -> sqlite3.Row:
    base: dict[str, Any] = {
        "id": iid, "op_id": "op-" + iid, "pool": pool, "provider": provider,
        "label": "llmhub-" + iid, "state": state, "state_since": T0, "created_at": T0,
        "max_lifetime_at": T0 + 10800,
    }  # fmt: skip
    base.update(cols)
    names = ",".join(base)
    conn.execute(
        f"INSERT INTO instances({names}) VALUES ({','.join('?' for _ in base)})",
        list(base.values()),
    )
    row = conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
    assert row is not None
    return row


def inst(conn: sqlite3.Connection, iid: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
    assert row is not None
    return row


def add_job(
    conn: sqlite3.Connection,
    jid: str,
    status: str = "queued",
    *,
    pool: str = POOL,
    instance_id: str | None = None,
    worker_id: str | None = None,
    attempts: int = 0,
    max_attempts: int = 2,
    lease: bool = True,
) -> None:
    conn.execute(
        "INSERT INTO jobs(id, client_id, body_sha256, created_at, priority, prio_rank,"
        " model_alias, policy, data_class, pool, status, route, est_tokens_out, req_path,"
        " instance_id, worker_id, attempts, max_attempts)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (jid, "c1", "h", T0, "normal", 1, "m1", "BALANCED", "private", pool, status, "cloud",
         100, "/x", instance_id, worker_id, attempts, max_attempts),
    )  # fmt: skip
    if lease:
        conn.execute(
            "INSERT INTO leases(id, holder, client_id, kind, pool, job_id, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            ("l-" + jid, "job:" + jid, "c1", "implicit", pool, jid, T0),
        )


def states(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["id"]: r["state"] for r in conn.execute("SELECT id, state FROM instances")}


def trail(conn: sqlite3.Connection, iid: str | None = None) -> list[tuple[str, str, str]]:
    sql = "SELECT from_state, to_state, reason FROM transitions"
    params: tuple[str, ...] = ()
    if iid:
        sql += " WHERE instance_id=?"
        params = (iid,)
    return [(r[0], r[1], r[2]) for r in conn.execute(sql + " ORDER BY id", params)]


@dataclass
class FakeRes:
    ref: str
    label: str
    kind: ResourceKind = ResourceKind.INSTANCE
    status: RemoteStatus = RemoteStatus.RUNNING
    spend: float | None = 0.3
    created_at: float = T0
    host_id: str = "host-1"
    hidden: bool = False  # exists and bills, but list_owned does not show it


class FakeCloud:
    """In-memory adapter with the faults stream B has not added to MockProvider yet."""

    name = "fake"
    capabilities = Capability.NATIVE_LABELS | Capability.SPEND_RATE

    def __init__(self, now: Callable[[], float] = lambda: T0) -> None:
        self.now = now
        self.resources: dict[str, FakeRes] = {}
        self.destroy_calls: list[str] = []
        self.destroy_fail = 0  # next N destroy calls raise
        self.destroy_retry_after: float | None = None
        self.destroy_never = False  # destroy is accepted but nothing is removed
        self.list_staleness_s = 0.0
        self.list_fail = False
        self.partial_volume = False  # destroying an instance leaves a volume with its label
        self.get_fail = False
        self._n = 0

    def add(self, label: str, **kw: Any) -> FakeRes:
        self._n += 1
        r = FakeRes(ref=f"r{self._n}", label=label, **kw)
        self.resources[r.ref] = r
        return r

    def _rr(self, r: FakeRes) -> RemoteResource:
        return RemoteResource(
            provider=self.name, ref=r.ref, label=r.label, kind=r.kind, status=r.status,
            endpoint=f"fake://{r.ref}", host_id=r.host_id, price_usd_h=0.3,
            spend_rate_usd_h=r.spend, created_at=r.created_at,
        )  # fmt: skip

    async def list_offers(self, q: OfferQuery) -> list[Offer]:
        return []

    async def create(self, spec: InstanceSpec, label: str) -> RemoteResource:
        return self._rr(self.add(label))

    async def get(self, ref: str) -> RemoteResource | None:
        if self.get_fail:
            raise RuntimeError("get failed")
        r = self.resources.get(ref)
        return None if r is None else self._rr(r)

    async def list_owned(self, prefix: str = "llmhub-") -> list[RemoteResource]:
        if self.list_fail:
            raise RuntimeError("list failed")
        return [
            self._rr(r)
            for r in self.resources.values()
            if r.label.startswith(prefix) and not r.hidden
        ]

    async def destroy(self, ref: str) -> DestroyResult:
        self.destroy_calls.append(ref)
        if self.destroy_fail > 0:
            self.destroy_fail -= 1
            raise ProviderError(
                NormalizedError(
                    reason=UnavailableReason.PROVIDER_API_ERROR, retryable=True,
                    outcome_unknown=False, retry_after_s=self.destroy_retry_after,
                    http_status=429 if self.destroy_retry_after else 503, message="destroy failed",
                )
            )  # fmt: skip
        if ref not in self.resources:
            return DestroyResult.ALREADY_GONE
        if not self.destroy_never:
            gone = self.resources.pop(ref)
            if self.partial_volume and gone.kind == ResourceKind.INSTANCE:
                self.partial_volume = False
                self.add(gone.label, kind=ResourceKind.VOLUME)
        return DestroyResult.ACCEPTED

    async def balance(self) -> Balance | None:
        return None

    async def billing_since(self, ts: float) -> list[BillingRecord]:
        return []

    def normalize_error(self, exc: BaseException) -> NormalizedError:
        if isinstance(exc, ProviderError):
            return exc.err
        return NormalizedError(
            reason=UnavailableReason.PROVIDER_API_ERROR, retryable=True, outcome_unknown=False,
            retry_after_s=None, http_status=None, message=type(exc).__name__,
        )  # fmt: skip


class FakeInference:
    def __init__(self) -> None:
        self.healthy = True
        self.reachable = True

    async def ping(self, endpoint: str) -> bool:
        return self.reachable

    async def health(self, endpoint: str) -> bool:
        return self.healthy and self.reachable

    async def test_request(self, endpoint: str) -> bool:
        return self.healthy

    async def run(self, endpoint: str, job: Any) -> Any:
        raise NotImplementedError
