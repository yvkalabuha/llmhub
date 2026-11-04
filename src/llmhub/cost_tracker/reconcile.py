"""Reconciliation of our ledger with provider balance and billing."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from llmhub.budget_manager import accounts
from llmhub.budget_manager.limits import windows
from llmhub.configuration.models import Config
from llmhub.cost_tracker.txutil import add_incident, join_or_open, state_get, state_set
from llmhub.persistence.db import assert_not_in_tx
from llmhub.persistence.state import ReconcileRequired
from llmhub.provider_adapters.base import BillingRecord, Capability, ProviderAdapter

LIVE_INTERVAL_S = 900.0
IDLE_INTERVAL_S = 86400.0
_KINDS = ("gpu", "disk", "traffic", "storage")


@dataclass(frozen=True)
class ReconcileResult:
    provider: str
    ours_usd: float
    provider_usd: float | None  # None without a billing API
    diverged: bool
    balance_usd: float | None
    records: int


def tolerance(provider_usd: float, cfg: Config) -> float:
    b = cfg.budget
    return max(b.divergence_abs, b.divergence_rel * provider_usd)


def reconcile_due(conn: sqlite3.Connection, provider: str, now: float, live: bool) -> bool:
    """Every 15 min with a live instance, otherwise once a day."""
    last = state_get(conn, f"last_reconcile_at:{provider}")
    interval = LIVE_INTERVAL_S if live else IDLE_INTERVAL_S
    return last is None or now - float(last) >= interval


def _kind(raw: str) -> str:
    return raw if raw in _KINDS else "gpu"


def _store_provider_rows(
    conn: sqlite3.Connection, provider: str, since: float, records: list[BillingRecord], now: float
) -> None:
    """Replace provider-source rows of the period (instance/pool filled when ref is known)."""
    conn.execute(
        "DELETE FROM ledger WHERE provider=? AND source='provider' AND to_ts > ?"
        " AND excluded_at IS NULL",
        (provider, since),
    )
    refs: dict[str, sqlite3.Row] = {}
    for rec in records:
        inst = None
        if rec.ref is not None:
            if rec.ref not in refs:
                row = conn.execute(
                    "SELECT id, pool FROM instances WHERE provider=? AND provider_ref=?",
                    (provider, rec.ref),
                ).fetchone()
                if row is not None:
                    refs[rec.ref] = row
            inst = refs.get(rec.ref)
        kind = _kind(rec.kind)
        conn.execute(
            "INSERT OR IGNORE INTO ledger(instance_id, provider, pool, kind, from_ts, to_ts,"
            " amount_usd, source, created_at) VALUES (?,?,?,?,?,?,?,'provider',?)",
            (
                None if inst is None else inst["id"], provider, None if inst is None else inst["pool"],
                kind, rec.from_ts, max(rec.to_ts, rec.from_ts), max(0.0, rec.amount_usd), now,
            ),
        )  # fmt: skip


def _ours_estimate(conn: sqlite3.Connection, provider: str, start: float, end: float) -> float:
    from llmhub.cost_tracker.ledger import PRORATA_SQL  # same pro-rating as spent()

    row = conn.execute(
        f"SELECT COALESCE(SUM({PRORATA_SQL}), 0) FROM ledger"
        " WHERE provider=:p AND source='estimate' AND to_ts > :s AND from_ts < :e"
        " AND excluded_at IS NULL",
        {"p": provider, "s": start, "e": end},
    ).fetchone()
    return float(row[0])


async def reconcile(
    conn: sqlite3.Connection, adapter: ProviderAdapter, now: float, cfg: Config
) -> ReconcileResult:
    """Snapshot of balance + billing since month start; ledger_divergence incident if needed.

    Provider calls happen outside any transaction; writes are one short transaction.
    """
    assert_not_in_tx()
    month_start = windows(now, cfg.budget.timezone).month_start
    balance_usd: float | None = None
    if Capability.BALANCE_API in adapter.capabilities:
        bal = await adapter.balance()
        balance_usd = None if bal is None else bal.usd
    records: list[BillingRecord] | None = None
    if Capability.BILLING_API in adapter.capabilities:
        records = await adapter.billing_since(month_start)
    provider_usd = None if records is None else sum(max(0.0, r.amount_usd) for r in records)
    diverged = False
    with join_or_open(conn):
        ours = _ours_estimate(conn, adapter.name, month_start, now)
        if balance_usd is not None:
            accounts.note_balance(conn, adapter.name, balance_usd, now)
        conn.execute(
            "INSERT INTO provider_snapshots(provider, ts, balance_usd, billed_usd_period,"
            " period_from) VALUES (?,?,?,?,?)",
            (adapter.name, now, balance_usd, provider_usd, month_start),
        )
        if records is not None and provider_usd is not None:
            _store_provider_rows(conn, adapter.name, month_start, records, now)
            diverged = abs(ours - provider_usd) > tolerance(provider_usd, cfg) + 1e-9
            if diverged:
                add_incident(
                    conn, now, "ledger_divergence", "warning", ref=adapter.name,
                    dedup_key=f"ledger_divergence:{adapter.name}:{int(now // 3600)}",
                    details_json=json.dumps(
                        {"ours": round(ours, 4), "provider": round(provider_usd, 4)}
                    ),
                )  # fmt: skip
        state_set(conn, f"last_reconcile_at:{adapter.name}", repr(now), now)
    return ReconcileResult(
        provider=adapter.name, ours_usd=ours, provider_usd=provider_usd, diverged=diverged,
        balance_usd=balance_usd, records=0 if records is None else len(records),
    )  # fmt: skip


# --- resume after DB corruption --------------------------------------------------------


def reconcile_required(conn: sqlite3.Connection) -> bool:
    """True after DB corruption until provider billing since month start was loaded."""
    reason = conn.execute(
        "SELECT updated_at FROM controller_state WHERE key='halt_reason' AND value='db_corrupt'"
    ).fetchone()
    flag = state_get(conn, "reconcile_required") == "1"
    if reason is None and not flag:
        return False
    loaded = conn.execute(
        "SELECT updated_at FROM controller_state WHERE key='billing_reloaded_at'"
    ).fetchone()
    if loaded is None:
        return True
    return reason is not None and float(loaded["updated_at"]) < float(reason["updated_at"])


def ensure_resume_allowed(conn: sqlite3.Connection) -> None:
    """Called by resume(): raises ReconcileRequired while billing is not reloaded."""
    if reconcile_required(conn):
        raise ReconcileRequired("load provider billing since month start before resume")


async def reload_billing(
    conn: sqlite3.Connection,
    adapters: Mapping[str, ProviderAdapter],
    now: float,
    cfg: Config,
) -> list[ReconcileResult]:
    """Load provider billing since month start (source=provider) for every adapter.

    Providers without a BILLING_API are noted by a warning incident. Any provider error
    propagates, so `billing_reloaded_at` is only set when all providers were read.
    """
    results = []
    for name, adapter in adapters.items():
        res = await reconcile(conn, adapter, now, cfg)
        results.append(res)
        if res.provider_usd is None:
            with join_or_open(conn):
                add_incident(
                    conn, now, "billing_unavailable", "warning", ref=name,
                    dedup_key=f"billing_unavailable:{name}:{int(now)}",
                )  # fmt: skip
    with join_or_open(conn):
        state_set(conn, "billing_reloaded_at", repr(now), now)
        state_set(conn, "db_reconciled", "1", now)  # persistence.state.unhalt checks it
    return results
