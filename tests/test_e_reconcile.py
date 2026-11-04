from __future__ import annotations

from pathlib import Path

import pytest

from llmhub.budget_manager.limits import Stage, stage, windows
from llmhub.cost_tracker.reconcile import (
    ReconcileRequired,
    ensure_resume_allowed,
    reconcile,
    reconcile_due,
    reconcile_required,
    reload_billing,
    tolerance,
)
from llmhub.provider_adapters.base import BillingRecord
from test_e_support import T0, FakeBillingAdapter, add_instance, add_ledger, make_cfg, make_conn


def rec(amount: float, ref: str | None = None, t: float = T0) -> BillingRecord:
    return BillingRecord(ref=ref, kind="gpu", from_ts=t, to_ts=t + 60, amount_usd=amount)


def incidents(conn) -> list[str]:  # type: ignore[no-untyped-def]
    return [r["kind"] for r in conn.execute("SELECT kind FROM incidents")]


async def test_within_tolerance_no_incident(tmp_path: Path) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path)
    add_ledger(conn, 0.50, T0 - 100, T0 - 40)
    ad = FakeBillingAdapter()
    ad.records = [rec(0.54, t=T0 - 100)]
    res = await reconcile(conn, ad, T0, cfg)
    assert not res.diverged and "ledger_divergence" not in incidents(conn)
    assert res.balance_usd == 20.0
    assert conn.execute("SELECT count(*) FROM provider_snapshots").fetchone()[0] == 1


async def test_divergence_incident_and_max(tmp_path: Path) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path)
    add_ledger(conn, 0.50, T0 - 100, T0 - 40)
    ad = FakeBillingAdapter()
    ad.records = [rec(0.56, t=T0 - 100)]  # 0.06 > max(0.05, 5 % of 0.56)
    res = await reconcile(conn, ad, T0, cfg)
    assert res.diverged and "ledger_divergence" in incidents(conn)
    from llmhub.cost_tracker.ledger import spent

    w = windows(T0, "UTC")
    assert spent(conn, w.month_start, w.month_end) == pytest.approx(0.56)  # limits use the max


async def test_provider_lower_than_estimate_keeps_estimate(tmp_path: Path) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path)
    add_ledger(conn, 1.0, T0 - 100, T0 - 40)
    ad = FakeBillingAdapter()
    ad.records = [rec(0.2, t=T0 - 100)]
    await reconcile(conn, ad, T0, cfg)
    from llmhub.cost_tracker.ledger import spent

    w = windows(T0, "UTC")
    assert spent(conn, w.month_start, w.month_end) == pytest.approx(1.0)


def test_tolerance_formula() -> None:
    cfg = make_cfg(Path("."))
    assert tolerance(0.5, cfg) == 0.05
    assert tolerance(10.0, cfg) == pytest.approx(0.5)


async def test_repeat_reconcile_replaces_provider_rows(tmp_path: Path) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path)
    ad = FakeBillingAdapter()
    ad.records = [rec(1.0)]
    await reconcile(conn, ad, T0 + 100, cfg)
    await reconcile(conn, ad, T0 + 200, cfg)
    n = conn.execute("SELECT count(*) FROM ledger WHERE source='provider'").fetchone()[0]
    assert n == 1


async def test_provider_rows_get_pool_from_ref(tmp_path: Path) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path)
    add_instance(conn, "i1", pool="x1")
    ad = FakeBillingAdapter()
    ad.records = [rec(1.0, ref="ref-i1")]
    await reconcile(conn, ad, T0 + 100, cfg)
    row = conn.execute("SELECT * FROM ledger WHERE source='provider'").fetchone()
    assert row["pool"] == "x1" and row["instance_id"] == "i1"


def test_reconcile_due_schedule(tmp_path: Path) -> None:
    from llmhub.cost_tracker.txutil import state_set

    conn = make_conn(tmp_path)
    assert reconcile_due(conn, "mock", T0, live=False)
    state_set(conn, "last_reconcile_at:mock", repr(T0), T0)
    assert not reconcile_due(conn, "mock", T0 + 899, live=True)
    assert reconcile_due(conn, "mock", T0 + 900, live=True)
    assert not reconcile_due(conn, "mock", T0 + 3600, live=False)
    assert reconcile_due(conn, "mock", T0 + 86400, live=False)


async def test_resume_refused_after_corruption_until_billing_loaded(tmp_path: Path) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path)
    conn.execute("INSERT INTO controller_state VALUES ('halted','1',?)", (T0,))
    conn.execute("INSERT INTO controller_state VALUES ('halt_reason','db_corrupt',?)", (T0,))
    assert reconcile_required(conn)
    with pytest.raises(ReconcileRequired):
        ensure_resume_allowed(conn)
    # fresh DB: month budget is empty; provider billing restores it
    assert stage(conn, T0 + 10, cfg) == Stage.NORMAL
    ad = FakeBillingAdapter()
    w = windows(T0, "UTC")
    ad.records = [rec(44.0, t=w.month_start)]
    await reload_billing(conn, {"mock": ad}, T0 + 10, cfg)
    ensure_resume_allowed(conn)  # no raise
    assert not reconcile_required(conn)
    rows = conn.execute("SELECT * FROM ledger WHERE source='provider'").fetchall()
    assert len(rows) == 1 and rows[0]["instance_id"] is None
    assert stage(conn, T0 + 10, make_cfg(tmp_path, budget_day=100.0)) == Stage.URGENT_ONLY


def test_no_corruption_no_requirement(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    assert not reconcile_required(conn)
    ensure_resume_allowed(conn)


async def test_new_corruption_requires_new_reload(tmp_path: Path) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path)
    conn.execute("INSERT INTO controller_state VALUES ('halt_reason','db_corrupt',?)", (T0,))
    await reload_billing(conn, {"mock": FakeBillingAdapter()}, T0 + 10, cfg)
    assert not reconcile_required(conn)
    conn.execute("UPDATE controller_state SET updated_at=? WHERE key='halt_reason'", (T0 + 1000,))
    assert reconcile_required(conn)
